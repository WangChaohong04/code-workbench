"""Engineering approval isolation, late cancellation guards and worker isolation."""
from datetime import datetime, timedelta, timezone
import json

from tests.conftest import workdir, tmp_db
from longflow import db, permissions as perm, safety
from longflow.models import CANCELLED, COMPLETED, WAITING_APPROVAL


def engineering_graph(session, *, extra_tasks=()):
    engine = session.engine()
    root_id = engine.create_goal('Document engineering review', 'engineering')['task_id']
    safety.confirm_contract(session.conn, root_id, actor='local', confidence='high',
                            allowed_tools=['record_note'])
    engine._materialize_plan(root_id, {'tasks': [
        {'role': 'executor', 'objective': 'Record reviewed evidence',
         'tool': 'record_note', 'tool_args': {'text': 'reviewed'}}, *extra_tasks]})
    return engine, root_id


def approve_exact(conn, task_id, args):
    perm.migrate_approval_schema(conn)
    epoch = safety.get_epoch(conn)
    row = conn.execute('SELECT root_id,workspace FROM tasks WHERE id=?', (task_id,)).fetchone()
    aid = db.new_id('ap')
    conn.execute('''INSERT INTO approvals
        (id,task_id,tool_name,args_json,args_hash,reason,status,created_at,
         root_id,workspace,epoch,environment_fingerprint,expires_at)
        VALUES (?,?,?,?,?,?,'approved',?,?,?,?,?,?)''',
        (aid, task_id, 'record_note', json.dumps(args), perm.args_hash('record_note', args),
         'Explicit reviewed test operation', db.now(), row['root_id'], row['workspace'], epoch,
         perm._approval_env('record_note', args, epoch, conn=conn, task_id=task_id),
         (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()))
    conn.commit()
    return aid


def test_approval_not_reusable_across_roots(workdir):
    session = tmp_db(workdir)
    _, ra = engineering_graph(session)
    _, rb = engineering_graph(session)
    ta = db.list_children(session.conn, ra)[0].id
    tb = db.list_children(session.conn, rb)[0].id
    args = {'text': 'reviewed'}
    approve_exact(session.conn, ta, args)
    assert perm.decide(session.conn, 'record_note', 'medium', args,
                       task_id=tb, root_id=rb).verdict == perm.NEEDS_APPROVAL
    assert perm.decide(session.conn, 'record_note', 'medium', args,
                       task_id=ta, root_id=ra).verdict == perm.ALLOW


def test_after_tool_does_not_overwrite_cancelled(workdir):
    session = tmp_db(workdir)
    engine, rid = engineering_graph(session)
    engine.tick(rid)
    task = db.list_children(session.conn, rid)[0]
    assert task.status == WAITING_APPROVAL
    engine.cancel(rid)
    engine._after_tool(task, 'record_note', {'recorded': True})
    assert db.get_task(session.conn, task.id).status == CANCELLED


def test_worker_engine_uses_own_connection(workdir):
    from longflow import api, config
    cfg = config.load_config()
    cfg['db_path'] = str(workdir / 'w.db')
    state = api.AppState(cfg)
    worker = state.worker_engine()
    try:
        assert worker.conn is not state.conn
        assert worker.runtime is not state.runtime
        assert worker.runtime.conn is worker.conn
    finally:
        worker.conn.close()
        state.conn.close()
