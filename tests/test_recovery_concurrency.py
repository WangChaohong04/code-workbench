"""Persisted engineering approvals, restart recovery, CAS and cancellation."""
from tests.conftest import tmp_db, workdir
from tests.test_governance_and_cancel_guard import engineering_graph, approve_exact
from longflow import db, orchestrator
from longflow.tools import ToolRuntime
from longflow.models import CANCELLED, COMPLETED, WAITING_APPROVAL, IN_PROGRESS, READY, PENDING


def _fresh_engine(session):
    conn = db.connect(str(session.db_path))
    db.init_db(conn)
    runtime = ToolRuntime(conn, session.registry)
    return orchestrator.Engine(conn, session.cfg, runtime), conn


def test_recovery_after_restart_completes_pending(workdir):
    session = tmp_db(workdir)
    engine, rid = engineering_graph(session)
    engine.tick(rid)
    task = db.list_children(session.conn, rid)[0]
    assert task.status == WAITING_APPROVAL
    restarted, conn = _fresh_engine(session)
    try:
        assert restarted.runtime.conn is conn
        restarted.tick(rid)
        assert db.get_task(conn, task.id).status == WAITING_APPROVAL
        pending = conn.execute("SELECT * FROM approvals WHERE task_id=? AND status='pending'",
                               (task.id,)).fetchone()
        assert pending and pending['epoch'] is not None and pending['expires_at']
        assert pending['environment_fingerprint']
        restarted.decide_approval(pending['id'], 'approved')
        restarted.tick(rid)
        assert db.get_task(conn, rid).status == COMPLETED
        assert conn.execute('SELECT status FROM approvals WHERE id=?',
                            (pending['id'],)).fetchone()['status'] == 'consumed'
    finally:
        conn.close()


def test_atomic_claim_prevents_double_pickup(workdir):
    session = tmp_db(workdir)
    tid = db.insert_task(session.conn, title='test', kind='task', agent_role='executor',
                         objective='Review code', root_id='r1', status=READY)
    assert db.transition_status(session.conn, tid, (PENDING, READY), IN_PROGRESS) is True
    assert db.transition_status(session.conn, tid, (PENDING, READY), IN_PROGRESS) is False
    assert db.get_task(session.conn, tid).status == IN_PROGRESS


def test_cancelled_root_not_overwritten_by_late_tick(workdir):
    session = tmp_db(workdir)
    engine, rid = engineering_graph(session)
    engine.tick(rid)
    assert db.get_task(session.conn, rid).status != COMPLETED
    engine.cancel(rid)
    restarted, conn = _fresh_engine(session)
    try:
        restarted.tick(rid)
        assert db.get_task(conn, rid).status == CANCELLED
        assert all(c.status == CANCELLED for c in db.list_children(conn, rid))
    finally:
        conn.close()


def test_cancel_records_irreversible_side_effects(workdir):
    session = tmp_db(workdir)
    engine, rid = engineering_graph(session, extra_tasks=[
        {'role': 'executor', 'objective': 'Second note', 'tool': 'record_note',
         'tool_args': {'text': 'still requires approval'}}])
    children = db.list_children(session.conn, rid)
    first = next(c for c in children if c.objective == 'Record reviewed evidence')
    approve_exact(session.conn, first.id, {'text': 'reviewed'})
    engine.tick(rid)
    assert db.get_task(session.conn, first.id).status == COMPLETED
    assert any(c.status == WAITING_APPROVAL for c in db.list_children(session.conn, rid))
    result = engine.cancel(rid)
    assert result['status'] == CANCELLED
    assert any(r.get('recorded') is True for r in result['irreversible_side_effects'])
    assert db.get_task(session.conn, first.id).status == COMPLETED
