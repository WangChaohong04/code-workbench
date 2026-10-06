"""Safety controls exercise persisted effects, not model wording."""
import sqlite3
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from longflow import db, safety
from longflow.permissions import PermissionDenied


@pytest.fixture
def state():
    # Avoid pytest tmpdir's restrictive Windows ACL mode in confined workers.
    directory = Path('.lfwork') / ('safety-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    path = str(directory / 'safety.sqlite')
    conn = db.init_db(db.connect(path))
    safety.init_schema(conn)
    root_id = db.insert_task(conn, title='Engineering', kind='root', agent_role='controller',
                             objective='Review repo', root_id='')
    task = db.get_task(conn, root_id)
    safety.ensure_contract(conn, root_id, 'Review repo')
    yield conn, task, path
    conn.close()
    shutil.rmtree(directory)


def spec(name, risk='low', side_effect=False):
    return SimpleNamespace(name=name, risk=risk, side_effect=side_effect)


def confirm(conn, task, **kwargs):
    return safety.confirm_contract(conn, task.root_id, actor='local',
                                   allowed_tools=['record_note'], confidence='high', **kwargs)


def test_contract_default_read_only_idempotent(state):
    conn, task, _ = state
    contract = safety.get_contract(conn, task.root_id)
    assert contract['mode'] == 'read_only'
    assert contract['confidence'] == 'low'
    assert contract['allowed_tools'] == []
    assert contract['confirmed_by'] is None
    confirmed = confirm(conn, task)
    assert safety.ensure_contract(conn, task.root_id, 'changed chat text') == confirmed
    with pytest.raises(PermissionDenied):
        safety.ensure_contract(conn, task.root_id, 'takeover', owner='other')


@pytest.mark.parametrize('confidence', ['low', 'medium'])
def test_non_high_confidence_cannot_write(state, confidence):
    conn, task, _ = state
    contract = safety.confirm_contract(conn, task.root_id, actor='local',
                                      allowed_tools=['record_note'], confidence=confidence)
    assert not contract['allowed_tools']
    with pytest.raises(PermissionDenied, match='confidence'):
        safety.check_tool(conn, task, 'record_note', {'text': 'hello'}, spec('record_note'))


@pytest.mark.parametrize('role', ['agent', 'controller', 'executor', 'researcher', 'system'])
def test_agents_cannot_authorize_or_unfreeze(state, role):
    conn, task, _ = state
    with pytest.raises(PermissionDenied):
        safety.confirm_contract(conn, task.root_id, actor='local', actor_role=role,
                                confidence='high', allowed_tools=['record_note'])
    with pytest.raises(PermissionDenied):
        safety.unfreeze(conn, actor='local', actor_role=role)
    with pytest.raises(PermissionDenied):
        safety.freeze(conn, actor='local', actor_role=role)


def test_wrong_owner_and_wildcard_rejected(state):
    conn, task, _ = state
    with pytest.raises(PermissionDenied):
        safety.confirm_contract(conn, task.root_id, actor='outsider')
    with pytest.raises(ValueError):
        safety.confirm_contract(conn, task.root_id, actor='local', allowed_tools=['*'])


def test_effect_risk_overrides_plugin_labels():
    assert safety.classify_operation('send_notification', {}, spec('send_notification'))['risk'] == 'high'
    assert safety.classify_operation('make_purchase', {}, spec('make_purchase'))['risk'] == 'high'
    assert safety.classify_operation('record_note', {}, spec('record_note'))['risk'] == 'medium'
    assert safety.classify_operation('http_get', {'method': 'DELETE'}, spec('http_get'))['risk'] == 'high'
    assert safety.classify_operation('http_get', {'method': 'POST'}, spec('http_get'))['risk'] == 'high'
    assert safety.classify_operation('kb_search', {}, spec('kb_search', 'high'))['risk'] == 'high'
    assert safety.classify_operation('kb_search', {}, spec('kb_search'))['risk'] == 'low'


def test_unknown_plugin_cannot_self_declare_controlled(state):
    conn, task, _ = state
    operation = safety.classify_operation('innocent_get', {}, spec('innocent_get'))
    assert operation['risk'] == 'high'
    assert not operation['controlled']
    with pytest.raises(PermissionDenied, match='uncontrolled'):
        safety.check_tool(conn, task, 'innocent_get', {}, spec('innocent_get'))
    with pytest.raises(PermissionDenied, match='uncontrolled'):
        safety.check_tool(conn, task, 'http_get', {'command': 'rm -rf'}, spec('http_get'))


def test_explicit_grants_deny_and_exact_resource_scope(state):
    conn, task, _ = state
    confirm(conn, task, resources={'path': ['notes/a.txt']})
    assert safety.check_tool(conn, task, 'record_note', {'path': 'notes/a.txt'}, spec('record_note'))['risk'] == 'medium'
    for path in ('notes/../secret', 'notes/b.txt', '/etc/passwd'):
        with pytest.raises(PermissionDenied, match='Resource'):
            safety.check_tool(conn, task, 'record_note', {'path': path}, spec('record_note'))
    confirm(conn, task, denied_tools=['record_note'])
    with pytest.raises(PermissionDenied, match='explicitly denied'):
        safety.check_tool(conn, task, 'record_note', {}, spec('record_note'))


def test_freeze_persists_epoch_revokes_and_does_not_fake_interrupt(state):
    conn, task, path = state
    confirm(conn, task)
    db.update_task(conn, task.id, status='in_progress')
    safety.record_execution(conn, task.root_id, 'running')
    for i, status in enumerate(('pending', 'approved', 'denied', 'consumed')):
        conn.execute('''INSERT INTO approvals(id,task_id,tool_name,args_json,args_hash,status,created_at)
                        VALUES(?,?,?,?,?,?,?)''',
                     (str(i), task.id, 'record_note', '{}', 'hash', status, db.now()))
    conn.commit()
    frozen = safety.freeze(conn, actor='local', reason='stop requested')
    assert frozen['epoch'] == 1
    assert frozen['running_operations'] == 'not_safely_interruptible'
    assert db.get_task(conn, task.id).status == 'in_progress'
    assert safety.snapshot(conn, task.id)['progress']['execution_status'] == 'running'
    assert [r[0] for r in conn.execute('SELECT status FROM approvals ORDER BY id')] == [
        'revoked', 'revoked', 'denied', 'consumed']
    other = db.connect(path)
    try:
        assert safety.is_frozen(other)
        assert safety.get_epoch(other) == 1
        with pytest.raises(PermissionDenied, match='freeze'):
            safety.check_tool(other, task, 'record_note', {}, spec('record_note'))
        assert safety.check_tool(other, task, 'kb_search', {}, spec('kb_search'))['risk'] == 'low'
        safety.unfreeze(other, actor='local')
        assert safety.get_epoch(conn) == 2
        with pytest.raises(PermissionDenied, match='stale'):
            safety.check_tool(conn, task, 'record_note', {}, spec('record_note'))
        confirm(conn, task)
        assert safety.check_tool(conn, task, 'record_note', {}, spec('record_note'))['epoch'] == 2
    finally:
        other.close()


def test_confirmation_frozen_and_contract_revision_invalidates_approvals(state):
    conn, task, _ = state
    safety.freeze(conn, actor='local')
    with pytest.raises(PermissionDenied, match='frozen'):
        confirm(conn, task)
    safety.unfreeze(conn, actor='local')
    conn.execute('''INSERT INTO approvals(id,task_id,tool_name,args_json,args_hash,status,created_at)
                    VALUES('a',?,'record_note','{}','x','approved',?)''', (task.id, db.now()))
    conn.commit()
    confirm(conn, task)
    assert conn.execute("SELECT status FROM approvals WHERE id='a'").fetchone()[0] == 'revoked'


def test_audit_logs_cannot_update_or_delete(state):
    conn, task, _ = state
    count = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    assert count > 0
    for sql in ("UPDATE events SET actor='attacker'", 'DELETE FROM events'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute(sql)
        conn.rollback()
    safety.init_schema(conn)
    assert conn.execute('SELECT COUNT(*) FROM events').fetchone()[0] == count
    with pytest.raises(PermissionDenied):
        safety.check_tool(conn, task, 'record_note', {}, spec('record_note'))
    assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='safety_tool_denied'").fetchone()[0] == 1


def test_execution_verification_recovery_are_independent(state):
    conn, task, _ = state
    safety.record_execution(conn, task.id, 'succeeded')
    safety.record_verification(conn, task.id, 'failed', detail='test failure')
    safety.record_recovery(conn, task.id, 'code', 'requested', actor='local')
    with pytest.raises(ValueError):
        safety.record_recovery(conn, task.id, 'code', 'succeeded', actor='local')
    with pytest.raises(PermissionDenied):
        safety.record_recovery_verified(conn, task.id, 'code', actor='local',
                                        actor_role='user', evidence={})
    with pytest.raises(ValueError):
        safety.record_recovery_verified(conn, task.id, 'code', actor='checker',
                                        actor_role='verifier', evidence={'passed': True})
    safety.record_recovery_verified(conn, task.id, 'code', actor='checker', actor_role='verifier',
                                    evidence={'passed': True, 'check': 'git tree matches',
                                              'artifact': 'checks/recovery.json'})
    view = safety.snapshot(conn, task.id)
    progress = view['progress']
    assert progress['execution_status'] == 'succeeded'
    assert progress['verification_status'] == 'failed'
    assert progress['code_recovery'] == 'succeeded'
    assert progress['config_recovery'] == progress['data_recovery'] == 'not_requested'
    assert view['chat_updates_allowed'] is False
    assert view['boundary'] == 'cooperative_in_process_not_os_sandbox'


def test_engineering_contract_is_structured_and_excludes_non_engineering(state):
    conn, task, _ = state
    engineering = {'repository': '/repo', 'branch': 'feature/a', 'modules': ['core'],
                   'allowed_dirs': ['core'], 'forbidden_dirs': ['secrets'],
                   'allow_dependencies': False, 'allow_config': False,
                   'database': 'none', 'environment': 'dev', 'acceptance': ['tests pass'],
                   'required_tests': ['pytest'], 'constraints': [], 'stop_conditions': ['freeze']}
    contract = confirm(conn, task, engineering=engineering)
    assert contract['engineering'] == engineering
    with pytest.raises(PermissionDenied, match='Non-engineering'):
        safety.confirm_contract(conn, task.id, actor='local', confidence='high',
                                allowed_tools=['make_purchase'], engineering=engineering)
