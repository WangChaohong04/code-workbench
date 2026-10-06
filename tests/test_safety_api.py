"""Authenticated, read-only status views and explicit safety control actions."""
from fastapi.testclient import TestClient
import pytest

from longflow import api, config, db, safety


@pytest.fixture
def control_client(workdir):
    cfg = config.load_config()
    cfg['db_path'] = str(workdir / 'safety-api.db')
    cfg['auth'] = {'tokens': {
        'alice': {'user': 'alice', 'role': 'member', 'workspaces': ['a']},
        'bob': {'user': 'bob', 'role': 'member', 'workspaces': ['a']},
        'other': {'user': 'other', 'role': 'member', 'workspaces': ['b']},
        'admin': {'user': 'admin', 'role': 'admin', 'workspaces': ['*']},
    }}
    app = api.create_app(cfg)
    conn = app.state.lf.conn
    root = db.insert_task(conn, title='Engineering', kind='goal', agent_role='controller',
                          objective='Inspect repository', root_id='', owner='alice', workspace='a')
    child = db.insert_task(conn, title='Child', kind='step', agent_role='researcher',
                           objective='Inspect', root_id=root, parent_id=root,
                           owner='alice', workspace='a')
    client = TestClient(app)
    yield client, conn, root, child
    client.close()
    if app.state.lf.runtime.http:
        app.state.lf.runtime.http.close()
    conn.close()


def headers(user='alice'):
    return {'Authorization': f'Bearer {user}'}


def test_owner_contract_and_child_snapshot(control_client):
    c, conn, root, child = control_client
    path = f'/api/tasks/{root}/contract'
    assert c.get(path, headers=headers()).status_code == 404
    assert safety.get_contract(conn, root) is None  # GET never creates authority
    r = c.post(path, headers=headers(), json={'engineering': {
        'repository': 'repo', 'allowed_dirs': ['longflow'], 'required_tests': ['pytest'],
        'allow_dependencies': False}})
    assert r.status_code == 200, r.text
    contract = r.json()
    assert contract['owner'] == 'alice' and contract['workspace'] == 'a'
    assert contract['confirmed_by'] == 'alice'
    assert contract['mode'] == 'read_only'
    view = c.get(f'/api/tasks/{child}/contract/snapshot', headers=headers()).json()
    assert view['contract']['root_id'] == root
    assert view['chat_updates_allowed'] is False
    assert c.get(path, headers=headers()).json() == contract


@pytest.mark.parametrize('suffix', ['contract', 'contract/snapshot', 'verification'])
def test_status_scoping(control_client, suffix):
    c, _, root, _ = control_client
    path = f'/api/tasks/{root}/{suffix}'
    assert c.get(path).status_code == 401
    assert c.get(path, headers=headers('bob')).status_code == 403
    assert c.get(path, headers=headers('other')).status_code == 404
    assert c.get('/api/tasks/missing/' + suffix, headers=headers()).status_code == 404


def test_control_body_rejects_identity_and_invalid_fields(control_client):
    c, _, root, _ = control_client
    path = f'/api/tasks/{root}/contract'
    for body in ({'actor': 'admin'}, {'confidence': 'certain'},
                 {'engineering': {'unknown': True}}, {'engineering': {'allow_config': 'yes'}},
                 {'allowed_tools': ['*']}):
        assert c.post(path, headers=headers(), json=body).status_code == 422
    assert c.post(path, headers=headers('admin'), json={}).status_code == 403
    assert c.post(path, headers=headers(), json={
        'confidence': 'high', 'allowed_tools': ['code_candidate'],
        'engineering': {'allowed_dirs': ['longflow']}}).status_code == 403
    assert c.post(path, headers=headers(), json={
        'confidence': 'high', 'allowed_tools': ['send_notification'],
        'engineering': {'repository': 'repo'}}).status_code == 403


def test_global_freeze_admin_only_and_stale_after_unfreeze(control_client):
    c, conn, root, _ = control_client
    path = f'/api/tasks/{root}/contract'
    grant = {'confidence': 'high', 'allowed_tools': ['record_note']}
    assert c.post(path, headers=headers(), json=grant).status_code == 200
    epoch = safety.get_contract(conn, root)['confirmed_epoch']
    for action in ('freeze', 'unfreeze'):
        assert c.post('/api/safety/' + action, headers=headers(), json={}).status_code == 403
        assert c.post('/api/safety/' + action, json={}).status_code == 401
    frozen = c.post('/api/safety/freeze', headers=headers('admin'), json={'reason': 'review'})
    assert frozen.status_code == 200
    assert frozen.json()['frozen'] is True
    assert frozen.json()['actor'] == 'admin'
    assert c.post(path, headers=headers(), json=grant).status_code == 403
    resumed = c.post('/api/safety/unfreeze', headers=headers('admin'))
    assert resumed.status_code == 200 and resumed.json()['frozen'] is False
    assert resumed.json()['epoch'] > epoch
    assert safety.get_contract(conn, root)['confirmed_epoch'] == epoch


def test_verification_is_independent_and_read_only(control_client):
    c, conn, root, _ = control_client
    c.post(f'/api/tasks/{root}/contract', headers=headers(), json={})
    safety.record_execution(conn, root, 'succeeded')
    path = f'/api/tasks/{root}/verification'
    view = c.get(path, headers=headers()).json()
    assert view['execution_status'] == 'succeeded'
    assert view['verification_status'] == 'not_started'
    assert c.post(path, headers=headers(), json={'status': 'passed'}).status_code == 405
    safety.record_verification(conn, root, 'inconclusive', detail='No test evidence')
    assert c.get(path, headers=headers()).json()['verification_status'] == 'inconclusive'
