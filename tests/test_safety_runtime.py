"""Exact-action approval regression tests (no real network or writes outside tmp_path)."""
from datetime import datetime, timedelta, timezone

from longflow import db, permissions as p, safety


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / 'runtime.sqlite'))
    db.init_db(conn)
    p.migrate_approval_schema(conn)
    safety.init_schema(conn)
    return conn


def _approval(conn, *, task='t1', epoch=0, expires=None):
    args = {'repository': 'repo', 'branch': 'main', 'diff': 'abc', 'environment': 'test'}
    expires = expires or (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    conn.execute('''INSERT INTO approvals
        (id,task_id,tool_name,args_json,args_hash,status,created_at,epoch,expires_at,environment_fingerprint)
        VALUES ('ap1',?,'code_write','{}',?,'approved',?,?,?,?)''',
        (task, p.args_hash('code_write', args), db.now(), epoch, expires,
         p._approval_env('code_write', args, epoch)))
    conn.commit()
    return args


def test_approval_single_use_and_exact_task(tmp_path):
    conn = _conn(tmp_path)
    args = _approval(conn)
    assert not p.consume_approval(conn, 'ap1', task_id='other', tool_name='code_write', args=args, epoch=0)
    assert p.consume_approval(conn, 'ap1', task_id='t1', tool_name='code_write', args=args, epoch=0)
    assert not p.consume_approval(conn, 'ap1', task_id='t1', tool_name='code_write', args=args, epoch=0)


def test_approval_epoch_and_environment_bound(tmp_path):
    conn = _conn(tmp_path)
    args = _approval(conn)
    assert not p.consume_approval(conn, 'ap1', task_id='t1', tool_name='code_write', args=args, epoch=1)
    assert not p.consume_approval(conn, 'ap1', task_id='t1', tool_name='code_write', args={**args, 'branch': 'other'}, epoch=0)


def test_medium_preauth_does_not_authorize(tmp_path):
    conn = _conn(tmp_path)
    p.add_grant(conn, scope='preauth', tool_name='code_write')
    assert p.decide(conn, 'code_write', 'medium', {}, task_id='t1').verdict == p.NEEDS_APPROVAL


def test_expired_approval_not_consumed(tmp_path):
    conn = _conn(tmp_path)
    args = _approval(conn, expires='2000-01-01T00:00:00+00:00')
    assert not p.consume_approval(conn, 'ap1', task_id='t1', tool_name='code_write', args=args, epoch=0)
