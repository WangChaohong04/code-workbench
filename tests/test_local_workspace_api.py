"""Local workspace writes require an exact, durable, single-use approval."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from longflow import api, config


ORIGINAL = 'answer = 1\n'
UPDATED = 'answer = 2\n'
PREFIX = '/api/local-workspaces'


def headers(token='alice', workspace='a', **extra):
    return {'Authorization': f'Bearer {token}', 'X-Workspace': workspace, **extra}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    from longflow import local_workspace_api

    monkeypatch.delenv('LONGFLOW_AUTH_TOKEN', raising=False)
    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src' / 'foo.py').write_bytes(ORIGINAL.encode('utf-8'))
    picker_calls = []

    def picker():
        picker_calls.append(True)
        return str(source)

    monkeypatch.setattr(local_workspace_api, 'pick_directory', picker)
    cfg = config.load_config()
    cfg['db_path'] = str(tmp_path / 'state' / 'local-api.db')
    cfg['auth'] = {'tokens': {
        'alice': {'user': 'alice', 'role': 'admin', 'workspaces': ['a', 'b']},
        'bob': {'user': 'bob', 'role': 'admin', 'workspaces': ['a']},
        'member': {'user': 'member', 'role': 'member', 'workspaces': ['a']},
    }}
    clients = []

    def close(client):
        client.close()
        state = client.app.state.lf
        if state.runtime.http:
            state.runtime.http.close()
        state.conn.close()
        clients.remove(client)

    def new_client(**kwargs):
        client = TestClient(api.create_app(cfg), **kwargs)
        clients.append(client)
        return client

    harness = SimpleNamespace(client=new_client(), source=source,
                              picker_calls=picker_calls, new_client=new_client,
                              close=close, module=local_workspace_api)
    yield harness
    for client in list(clients):
        close(client)


def select(harness):
    return ok(harness.client.post(PREFIX + '/pick', headers=headers()))['workspace_id']


def stage(harness, wid, changes=None):
    return ok(harness.client.post(f'{PREFIX}/{wid}/candidates', headers=headers(),
                                  json={'changes': changes if changes is not None
                                        else {'src/foo.py': UPDATED}}))


def approval_body(candidate, **overrides):
    return {'candidate_id': candidate['candidate_id'],
            'source_hash': candidate['source_hash'],
            'candidate_hash': candidate['candidate_hash'],
            'confirmed': True, **overrides}


def approve(harness, wid, candidate):
    return ok(harness.client.post(f'{PREFIX}/{wid}/approvals', headers=headers(),
                                 json=approval_body(candidate)))['approval_id']


def apply(harness, wid, approval_id):
    return harness.client.post(f'{PREFIX}/{wid}/apply', headers=headers(),
                               json={'approval_id': approval_id})


def source_files(harness):
    return {p.relative_to(harness.source).as_posix(): p.read_bytes()
            for p in harness.source.rglob('*') if p.is_file()}


def test_pick_cancelled_without_project(local_client, monkeypatch):
    monkeypatch.setattr(local_client.module, 'pick_directory', lambda: '')
    assert local_client.client.post(PREFIX + '/pick', headers=headers()).status_code == 409
    assert local_client.client.app.state.lf.conn.execute(
        'SELECT COUNT(*) FROM local_workspaces').fetchone()[0] == 0
    # Cancellation must release the picker lock for the next request.
    monkeypatch.setattr(local_client.module, 'pick_directory', lambda: str(local_client.source))
    select(local_client)


@pytest.mark.parametrize('extra', [
    {'Origin': 'https://attacker.invalid'}, {'Origin': 'null'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_cross_site_pick_rejected_before_native_picker(local_client, extra):
    assert local_client.client.post(PREFIX + '/pick', headers=headers(**extra)).status_code == 403
    assert local_client.picker_calls == []


def test_nonlocal_requests_denied(local_client):
    wid = select(local_client)
    remote = local_client.new_client(client=('203.0.113.8', 40000))
    assert remote.post(PREFIX + '/pick', headers=headers()).status_code == 403
    assert remote.get(f'{PREFIX}/{wid}', headers=headers()).status_code == 403
    assert remote.post(f'{PREFIX}/{wid}/candidates', headers=headers(),
                       json={'changes': {'src/foo.py': UPDATED}}).status_code == 403
    assert len(local_client.picker_calls) == 1
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}


def test_pick_requires_authenticated_admin(local_client):
    assert local_client.client.post(PREFIX + '/pick').status_code == 401
    assert local_client.client.post(PREFIX + '/pick', headers=headers('member')).status_code == 403
    assert local_client.picker_calls == []


@pytest.mark.parametrize('token,workspace', [('bob', 'a'), ('alice', 'b')])
def test_token_owner_and_workspace_isolation(local_client, token, workspace):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    aid = approve(local_client, wid, candidate)
    c = local_client.client
    foreign = headers(token, workspace)
    for suffix, params in [('', {}), ('/file', {'path': 'src/foo.py'}), ('/versions', {})]:
        assert c.get(f'{PREFIX}/{wid}{suffix}', headers=foreign, params=params).status_code == 404
    for suffix, body in [('/candidates', {'changes': {'src/foo.py': 'foreign\n'}}),
                         ('/approvals', approval_body(candidate)),
                         ('/apply', {'approval_id': aid})]:
        assert c.post(f'{PREFIX}/{wid}{suffix}', headers=foreign, json=body).status_code == 404
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}
    ok(apply(local_client, wid, aid))  # Foreign requests never consume the owner's approval.


def test_read_selected_file_without_mutation(local_client):
    wid = select(local_client)
    info = ok(local_client.client.get(f'{PREFIX}/{wid}', headers=headers()))
    assert info['workspace_id'] == wid
    assert {'path': 'src/foo.py'} in info['files']
    before = source_files(local_client)
    result = ok(local_client.client.get(f'{PREFIX}/{wid}/file', headers=headers(),
                                       params={'path': 'src/foo.py'}))
    assert result == {'path': 'src/foo.py', 'content': ORIGINAL}
    assert source_files(local_client) == before


@pytest.mark.parametrize('changes', [
    {'src/added.py': 'added = True\n'}, {'src/foo.py': UPDATED}, {'src/foo.py': None},
])
def test_stage_add_edit_delete_leaves_source_unchanged(local_client, changes):
    wid = select(local_client)
    before = source_files(local_client)
    initial = ok(local_client.client.get(f'{PREFIX}/{wid}', headers=headers()))
    candidate = stage(local_client, wid, changes)
    assert candidate['candidate_id']
    assert candidate['source_hash'] == initial['source_hash']
    assert candidate['candidate_hash'] != candidate['source_hash']
    assert candidate['changed_files'] == sorted(changes)
    assert set(candidate['before_manifest']) == {'src/foo.py'}
    expected = dict(before)
    for path, content in changes.items():
        if content is None:
            expected.pop(path)
        else:
            expected[path] = content.encode()
    assert set(candidate['after_manifest']) == set(expected)
    assert candidate['unified_diff']
    assert source_files(local_client) == before


@pytest.mark.parametrize('confirmed', [False, None])
def test_approval_requires_explicit_confirmation(local_client, confirmed):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    body = approval_body(candidate, confirmed=confirmed)
    if confirmed is None:
        body.pop('confirmed')
    assert local_client.client.post(f'{PREFIX}/{wid}/approvals', headers=headers(),
                                    json=body).status_code == 422
    assert local_client.client.app.state.lf.conn.execute(
        'SELECT COUNT(*) FROM local_write_approvals').fetchone()[0] == 0
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}


@pytest.mark.parametrize('field', ['source_hash', 'candidate_hash'])
def test_approval_binds_exact_source_and_candidate_hashes(local_client, field):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    assert local_client.client.post(f'{PREFIX}/{wid}/approvals', headers=headers(),
        json=approval_body(candidate, **{field: '0' * 64})).status_code == 409
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}
    aid = approve(local_client, wid, candidate)
    row = local_client.client.app.state.lf.conn.execute(
        'SELECT * FROM local_write_approvals WHERE id=?', (aid,)).fetchone()
    assert row['source_hash'] == candidate['source_hash']
    assert row['candidate_hash'] == candidate['candidate_hash']
    assert row['candidate_id'] == candidate['candidate_id']
    ok(apply(local_client, wid, aid))
    assert source_files(local_client) == {'src/foo.py': UPDATED.encode()}


def test_approval_is_single_use(local_client):
    wid = select(local_client)
    aid = approve(local_client, wid, stage(local_client, wid))
    ok(apply(local_client, wid, aid))
    assert apply(local_client, wid, aid).status_code == 403
    assert source_files(local_client) == {'src/foo.py': UPDATED.encode()}


def test_freeze_and_unfreeze_invalidates_existing_approval(local_client):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    aid = approve(local_client, wid, candidate)
    c = local_client.client
    ok(c.post('/api/safety/freeze', headers=headers(), json={'reason': 'review'}))
    assert apply(local_client, wid, aid).status_code == 409
    assert c.post(f'{PREFIX}/{wid}/candidates', headers=headers(),
                  json={'changes': {'src/foo.py': UPDATED}}).status_code == 409
    assert c.post(f'{PREFIX}/{wid}/approvals', headers=headers(),
                  json=approval_body(candidate)).status_code == 409
    assert c.post(f'{PREFIX}/{wid}/restore-preview', headers=headers(),
                  json={'commit': 'unused'}).status_code == 409
    assert ok(c.get(f'{PREFIX}/{wid}/file', headers=headers(),
                    params={'path': 'src/foo.py'}))['content'] == ORIGINAL
    ok(c.post('/api/safety/unfreeze', headers=headers()))
    assert apply(local_client, wid, aid).status_code == 403
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}
    ok(apply(local_client, wid, approve(local_client, wid, candidate)))


@pytest.mark.parametrize('when', ['before_approval', 'before_application'])
def test_external_source_drift_rejected(local_client, when):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    aid = approve(local_client, wid, candidate) if when == 'before_application' else None
    (local_client.source / 'src' / 'foo.py').write_text('external = True\n', encoding='utf-8')
    before = source_files(local_client)
    if aid:
        assert apply(local_client, wid, aid).status_code == 409
        assert apply(local_client, wid, aid).status_code == 403
    else:
        assert local_client.client.post(f'{PREFIX}/{wid}/approvals', headers=headers(),
            json=approval_body(candidate)).status_code == 409
    assert source_files(local_client) == before


def test_versions_restore_preview_leaves_source_untouched_until_approved(local_client):
    wid = select(local_client)
    c = local_client.client
    versions = ok(c.get(f'{PREFIX}/{wid}/versions', headers=headers()))['versions']
    assert len(versions) == 1
    initial = versions[0]
    assert ok(c.get(f'{PREFIX}/{wid}/version', headers=headers(),
                    params={'commit': initial['commit'], 'path': 'src/foo.py'}))['content'] == ORIGINAL
    candidate = stage(local_client, wid)
    applied = ok(apply(local_client, wid, approve(local_client, wid, candidate)))
    versions = ok(c.get(f'{PREFIX}/{wid}/versions', headers=headers()))['versions']
    assert {v['commit'] for v in versions} == {initial['commit'], applied['commit']}
    before = source_files(local_client)
    diff = ok(c.get(f'{PREFIX}/{wid}/diff', headers=headers(), params={
        'from_commit': initial['commit'], 'to_commit': applied['commit']}))
    assert diff['source_hash'] == initial['source_hash']
    assert diff['candidate_hash'] == candidate['candidate_hash']
    assert diff['changed_files'] == ['src/foo.py']
    restored = ok(c.post(f'{PREFIX}/{wid}/restore-preview', headers=headers(),
                         json={'commit': initial['commit']}))
    assert restored['restored_commit'] == initial['commit']
    assert restored['source_hash'] == candidate['candidate_hash']
    assert restored['candidate_hash'] == initial['source_hash']
    assert source_files(local_client) == before
    assert ok(c.get(f'{PREFIX}/{wid}/version', headers=headers(),
                    params={'commit': applied['commit'], 'path': 'src/foo.py'}))['content'] == UPDATED
    assert source_files(local_client) == before
    aid = approve(local_client, wid, restored)
    assert source_files(local_client) == before
    ok(apply(local_client, wid, aid))
    assert source_files(local_client) == {'src/foo.py': ORIGINAL.encode()}


def test_restart_preserves_project_candidate_and_approval(local_client):
    wid = select(local_client)
    candidate = stage(local_client, wid)
    local_client.close(local_client.client)
    local_client.client = local_client.new_client()
    info = ok(local_client.client.get(f'{PREFIX}/{wid}', headers=headers()))
    assert info['source_hash'] == candidate['source_hash']
    aid = approve(local_client, wid, candidate)
    local_client.close(local_client.client)
    local_client.client = local_client.new_client()
    ok(apply(local_client, wid, aid))
    assert source_files(local_client) == {'src/foo.py': UPDATED.encode()}
    assert apply(local_client, wid, aid).status_code == 403
