"""Retained run API contract tests; no autonomous executor or original writes."""
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from longflow import local_workspace_api as router


BASE = '/api/local-workspaces/w/runs'
PLAN = {'subtasks': [{'id': 'edit', 'deps': [], 'objective': 'Update answer',
                     'acceptance': ['Answer is two'], 'allowed_paths': ['src/foo.py'],
                     'presets': ['test', 'lint']}],
        'context': {'manifest': {'src/foo.py': {}}, 'omitted_files': 0,
                    'truncated_paths': []}, 'evidence': {'fallback': False}}


@pytest.fixture
def harness(monkeypatch):
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    current = {'hash': 'baseline', 'epoch': 0, 'frozen': False}
    calls = []
    hook = {'analyze': None, 'propose': None}
    candidate = {'candidate_id': 'candidate', 'candidate_hash': 'candidate-hash',
                 'source_hash': 'baseline', 'changed_files': ['src/foo.py']}

    class Workspace:
        def __init__(self, *args):
            pass

        def info(self):
            return {'source_hash': current['hash']}

        def preview(self, cid):
            assert cid == 'candidate'
            return copy.deepcopy(candidate)

    class AI:
        def __init__(self, driver):
            pass

        def plan(self, workspace, goal):
            calls.append(('plan', goal))
            return copy.deepcopy(PLAN)

        def analyze(self, workspace, goal):
            calls.append(('analyze', goal))
            if hook['analyze']:
                hook['analyze']()
            return {'summary': 'analysis', 'evidence': {'fallback': False}}

        def propose(self, workspace, goal, paths, *, check_write):
            calls.append(('propose', paths))
            if hook['propose']:
                hook['propose']()
            check_write()
            return copy.deepcopy(candidate)

    from longflow import engineering_ai
    monkeypatch.setattr(router, 'LocalWorkspace', Workspace)
    monkeypatch.setattr(engineering_ai, 'EngineeringAI', AI)
    monkeypatch.setattr(router.safety, 'get_epoch', lambda conn: current['epoch'])
    monkeypatch.setattr(router.safety, 'is_frozen', lambda conn: current['frozen'])
    state = SimpleNamespace(conn=conn, cfg={}, engine=lambda: SimpleNamespace(driver=object()))

    def principal(request: Request):
        user = request.headers.get('authorization')
        if not user:
            raise HTTPException(401, 'Authentication required')
        return SimpleNamespace(user=user, workspace=request.headers.get('x-workspace', 'a'))

    def client():
        app = FastAPI()
        router.install(app, state, principal)
        return TestClient(app)

    c = client()
    conn.execute('INSERT INTO local_workspaces VALUES (?,?,?,?,?)', ('w', 'alice', 'a', 'source', 'storage'))
    conn.execute('INSERT INTO local_workspaces VALUES (?,?,?,?,?)', ('other', 'alice', 'a', 'source', 'storage'))
    conn.commit()
    h = SimpleNamespace(client=c, conn=conn, current=current, calls=calls, hook=hook,
                        candidate=candidate, client_factory=client)
    yield h
    c.close()
    conn.close()


def auth(user='alice', workspace='a', **extra):
    return {'Authorization': user, 'X-Workspace': workspace, **extra}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def create(h):
    return ok(h.client.post(BASE, headers=auth(), json={
        'goal': 'Update answer', 'confirmed_send_code': True}))


def action(h, rid, name, body=None):
    return h.client.post(f'{BASE}/{rid}/{name}', headers=auth(), json=body or {})


def confirm(h, rid):
    return ok(action(h, rid, 'confirm', {'confirmed': True, 'allowed_paths': ['src/foo.py']}))


def step(h, rid):
    return action(h, rid, 'step', {'confirmed_send_code': True})


def test_lazy_schema_and_honest_capabilities(harness):
    h = harness
    assert h.conn.execute("SELECT name FROM sqlite_master WHERE name='engineering_runs'").fetchone() is None
    capabilities = ok(h.client.get(BASE, headers=auth()))
    assert capabilities['advancement'] == 'manual'
    assert capabilities['background_worker'] is False
    assert capabilities['auto_apply'] is False
    assert capabilities['execution']['available'] is False
    assert capabilities['record_applied']['available'] is True
    assert capabilities['record_applied']['reason'] == 'server-derived local workspace receipt'
    assert h.calls == []


def test_create_preserves_plan_confirmation_and_restart(harness):
    h = harness
    run = create(h)
    assert run['plan'] == PLAN
    assert run['confirmed'] is False
    assert run['status'] == 'waiting_user'
    assert h.calls == [('plan', 'Update answer')]
    assert action(h, run['id'], 'confirm').status_code == 422
    assert step(h, run['id']).status_code == 409
    confirm(h, run['id'])
    replacement = h.client_factory()
    try:
        restored = ok(replacement.get(f"{BASE}/{run['id']}", headers=auth()))
        assert restored['plan'] == PLAN
        assert restored['confirmed'] is True
    finally:
        replacement.close()


@pytest.mark.parametrize('body', [
    {'goal': 'Goal'}, {'goal': 'Goal', 'confirmed_send_code': 'true'},
    {'goal': 'Goal', 'confirmed_send_code': True, 'plan': {'nodes': []}},
    {'goal': 'Goal', 'confirmed_send_code': True, 'commands': ['echo fake']},
    {'goal': 'Goal', 'confirmed_send_code': True, 'maxsteps': True},
])
def test_strict_create_no_request_commands(harness, body):
    assert harness.client.post(BASE, headers=auth(), json=body).status_code == 422
    assert harness.calls == []


@pytest.mark.parametrize('paths', [[], ['src/other.py'], ['src/foo.py', 'src/other.py'],
                                   ['src/foo.py', 'src/foo.py']])
def test_confirmation_requires_exact_reviewed_paths(harness, paths):
    rid = create(harness)['id']
    assert action(harness, rid, 'confirm', {'confirmed': True}).status_code == 422
    assert action(harness, rid, 'confirm', {
        'confirmed': True, 'allowed_paths': paths}).status_code == 422
    assert harness.conn.execute('SELECT COUNT(*) FROM local_engineering_run_scopes').fetchone()[0] == 0


def test_missing_persisted_reviewed_scope_blocks_step(harness):
    rid = create(harness)['id']
    confirm(harness, rid)
    harness.conn.execute('DELETE FROM local_engineering_run_scopes WHERE run_id=?', (rid,))
    harness.conn.commit()
    assert step(harness, rid).status_code == 409
    assert harness.calls == [('plan', 'Update answer')]


def test_owner_workspace_and_local_isolation(harness):
    h = harness
    run = create(h)
    url = f"{BASE}/{run['id']}"
    assert h.client.get(url).status_code == 401
    for headers in (auth('bob'), auth(workspace='b')):
        assert h.client.get(url, headers=headers).status_code == 404
        for route in ('confirm', 'step', 'pause', 'resume', 'stop', 'retry'):
            body = {'confirmed': True, 'allowed_paths': ['src/foo.py']} if route == 'confirm' else (
                {'confirmed_send_code': True} if route == 'step' else (
                    {'node_id': 'edit', 'confirmed': True} if route == 'retry' else {}))
            assert h.client.post(f'{url}/{route}', headers=headers, json=body).status_code == 404
    assert h.client.get(url.replace('/w/', '/other/'), headers=auth()).status_code == 404
    assert h.client.get(url, headers=auth(Origin='https://attacker.invalid')).status_code == 403
    remote = TestClient(h.client.app, client=('203.0.113.8', 40000))
    try:
        assert remote.get(url, headers=auth()).status_code == 403
        assert remote.post(BASE, headers=auth(), json={
            'goal': 'Goal', 'confirmed_send_code': True}).status_code == 403
    finally:
        remote.close()


def test_manual_stages_use_plan_paths_and_executor_is_blocked(harness):
    h = harness
    run = create(h)
    rid = run['id']
    confirm(h, rid)
    assert action(h, rid, 'step').status_code == 422
    analyzed = ok(step(h, rid))
    assert analyzed['nodes'][0]['phase'] == 'propose'
    assert 'Answer is two' in h.calls[-1][1]
    proposed = ok(step(h, rid))
    assert h.calls[-1] == ('propose', ['src/foo.py'])
    assert proposed['nodes'][0]['candidate']['candidate_hash'] == 'candidate-hash'
    blocked = ok(step(h, rid))
    assert blocked['status'] == 'blocked'
    assert blocked['reason'] == 'executor_unavailable_or_unapproved'
    assert blocked['nodes'][0]['phase'] == 'verify'
    assert blocked['nodes'][0]['status'] != 'waiting_user'
    # The endpoint exists, but cannot attest application before a server-derived receipt.
    assert action(h, rid, 'record-applied', {'node_id': 'task-a', 'confirmed': True}).status_code == 409
    assert action(h, rid, 'record-applied', {'verified': True}).status_code == 422
    assert action(h, rid, 'step', {'confirmed_send_code': True, 'checks': []}).status_code == 422


def test_pause_resume_stop_are_explicit_and_strict(harness):
    h = harness
    rid = create(h)['id']
    confirm(h, rid)
    assert ok(action(h, rid, 'pause'))['status'] == 'paused'
    assert step(h, rid).status_code == 409
    assert ok(action(h, rid, 'resume'))['status'] == 'pending'
    assert action(h, rid, 'stop', {'command': 'anything'}).status_code == 422
    assert ok(action(h, rid, 'stop'))['status'] == 'stopped'
    assert step(h, rid).status_code == 409


@pytest.mark.parametrize('change', ['hash', 'epoch'])
def test_each_binding_uses_current_source_and_epoch(harness, change):
    h = harness
    rid = create(h)['id']
    h.current[change] = 'changed' if change == 'hash' else 1
    assert h.client.get(f'{BASE}/{rid}', headers=auth()).status_code == 409
    assert action(h, rid, 'confirm', {'confirmed': True, 'allowed_paths': ['src/foo.py']}).status_code == 409


def test_freeze_before_step_does_not_call_ai(harness):
    h = harness
    rid = create(h)['id']
    confirm(h, rid)
    h.current['frozen'] = True
    assert step(h, rid).status_code == 409
    assert h.calls == [('plan', 'Update answer')]


@pytest.mark.parametrize('phase', ['analyze', 'propose'])
def test_late_freeze_is_not_success(harness, phase):
    h = harness
    rid = create(h)['id']
    confirm(h, rid)
    if phase == 'propose':
        ok(step(h, rid))
    h.hook[phase] = lambda: h.current.update(frozen=True, epoch=1)
    assert step(h, rid).status_code == 409
    persisted = h.conn.execute('SELECT state FROM engineering_runs WHERE id=?', (rid,)).fetchone()[0]
    node = json.loads(persisted)['nodes'][0]
    assert node['status'] != 'completed'
    assert node['candidate'] is None


def test_final_cas_guard_discards_result_after_callback(harness, monkeypatch):
    from longflow.engineering_runs import EngineeringRunService
    original = EngineeringRunService.step

    def guarded_step(self, *args, safety_guard=None, **kwargs):
        def freeze_before_commit():
            harness.current.update(frozen=True, epoch=1)
            return safety_guard()
        return original(self, *args, safety_guard=freeze_before_commit, **kwargs)

    monkeypatch.setattr(EngineeringRunService, 'step', guarded_step)
    rid = create(harness)['id']
    confirm(harness, rid)
    assert step(harness, rid).status_code == 409
    persisted = harness.conn.execute('SELECT state FROM engineering_runs WHERE id=?', (rid,)).fetchone()[0]
    run = json.loads(persisted)
    assert run['status'] == 'blocked'
    assert run['nodes'][0]['status'] == 'in_doubt'
    assert run['nodes'][0]['phase'] == 'analyze'


def test_server_preview_rejects_proposal_scope_expansion(harness):
    h = harness
    rid = create(h)['id']
    confirm(h, rid)
    ok(step(h, rid))
    h.candidate['changed_files'] = ['src/unauthorized.py']
    failed = ok(step(h, rid))
    assert failed['nodes'][0]['status'] == 'failed'
    assert failed['nodes'][0]['candidate'] is None
    assert action(h, rid, 'retry', {'node_id': 'edit'}).status_code == 422
    repaired = ok(action(h, rid, 'retry', {'node_id': 'edit', 'confirmed': True}))
    assert repaired['nodes'][0]['phase'] == 'analyze'
    assert repaired['nodes'][0]['repair_count'] == 1


def test_pause_during_callback_revokes_late_output(harness):
    h = harness
    rid = create(h)['id']
    confirm(h, rid)
    h.hook['analyze'] = lambda: ok(action(h, rid, 'pause'))
    assert step(h, rid).status_code == 409
    result = ok(h.client.get(f'{BASE}/{rid}', headers=auth()))
    assert result['status'] == 'paused'
    assert result['nodes'][0]['status'] == 'in_doubt'
