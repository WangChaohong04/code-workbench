"""Contract tests for the standalone engineering operations HTTP adapter.

The integration suite supplies the configured app and native repository; these
small tests keep request schemas strict and verify unavailable-by-default
behavior without executing Git or recovery operations.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from longflow import operations_api
from longflow.engineering_operations import EngineeringOperations, TrustedRepository
from longflow.operations_api import ExecuteBody, ProposalBody
from longflow.production_adapters import ProductionAdapters


def test_git_branch_request_requires_exact_fields():
    body = ProposalBody(action="git_branch", branch="feature", base_branch="main")
    assert body.model_dump(exclude_none=True) == {
        "action": "git_branch", "branch": "feature", "base_branch": "main"
    }


def test_git_action_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ProposalBody(action="git_branch", branch="feature", base_branch="main", extra="x")


def test_execution_requires_approval_and_push_confirmation_is_separate():
    body = ExecuteBody(approval_id="approval")
    assert body.approval_id == "approval"
    assert body.confirmed is False
    assert body.confirm_push is False


@pytest.mark.parametrize("confirmed", [1, "true", None])
def test_execution_confirmation_requires_strict_boolean(confirmed):
    with pytest.raises(ValidationError):
        ExecuteBody(approval_id="approval", confirmed=confirmed)


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    sources = {"alice": tmp_path / "alice", "bob": tmp_path / "bob"}
    workspaces = {
        str(source): SimpleNamespace(source=source, storage=tmp_path / (owner + "-storage"))
        for owner, source in sources.items()
    }

    class Connection:
        def execute(self, query, params):
            wid, owner, workspace = params
            row = None
            if wid == "local" and owner in sources and workspace == "project":
                row = {"source": str(sources[owner]), "storage": "unused"}
            return SimpleNamespace(fetchone=lambda: row)

    class Service(EngineeringOperations):
        def __init__(self, *, repository=None, workspace=None, **kwargs):
            super().__init__(workspace=workspace, **kwargs)
            self.repository = repository
            self.root = Path(repository.root) if repository else None
            self.calls = []
            self.fail = False

        def propose_branch(self, branch, base_branch):
            return self._proposal("git_branch", {"root": str(self.root)})

        def propose_recovery(self):
            assert self.repository is None
            assert self.workspace is not None
            assert self.check_write() is False
            return self._proposal("local_recovery", {"source": str(self.workspace.source)})

        def execute(self, proposal):
            self.calls.append(proposal)
            assert self.consume_approval(proposal) is True
            assert self.confirm_push(proposal) is False
            if self.fail:
                raise RuntimeError("execution failed")
            return {"status": "completed", "action": proposal.action}

    monkeypatch.setattr(operations_api, "EngineeringOperations", Service)
    monkeypatch.setattr(operations_api, "LocalWorkspace", lambda source, storage: workspaces[source])
    monkeypatch.setattr(operations_api.safety, "get_epoch", lambda conn: 0)
    monkeypatch.setattr(operations_api.safety, "is_frozen", lambda conn: False)
    state = SimpleNamespace(conn=Connection(), cfg={})
    app = FastAPI()

    def principal(request: Request):
        return SimpleNamespace(user=request.headers.get("x-owner", "alice"), workspace="project")

    operations_api.install(app, state, principal)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, Service=Service, sources=sources)


def branch_preview(adapter, owner="alice"):
    return adapter.client.post(
        "/api/local-workspaces/local/git/proposals", headers={"x-owner": owner},
        json={"action": "git_branch", "branch": "feature", "base_branch": "main"},
    )


def test_production_capabilities_fail_closed(adapter):
    response = adapter.client.get('/api/local-workspaces/local/production/capabilities')
    assert response.status_code == 200
    result = response.json()
    for name in ('ci_cd', 'production_deployment', 'database_migration', 'database_restore'):
        assert result[name]['status'] == 'not_connected'
    assert result['automatic_execution'] is False
    assert result['provider_execution_wired'] is False
    assert result['approval_required'] is True


def test_production_capabilities_preserves_blocked_status(adapter):
    production = ProductionAdapters()
    production.capabilities = lambda: {'production_deployment': {'status': 'blocked'}}
    adapter.state.production_adapters = production
    response = adapter.client.get('/api/local-workspaces/local/production/capabilities')
    assert response.status_code == 200
    assert response.json()['production_deployment']['status'] == 'blocked'


@pytest.mark.parametrize('invalid_status', ['completed', 3, None, [], {}])
def test_production_capabilities_hides_non_mapping_status(adapter, invalid_status):
    production = ProductionAdapters()
    production.capabilities = lambda: {'production_deployment': {'status': invalid_status}, 'production_database': {'status': invalid_status}}
    adapter.state.production_adapters = production
    response = adapter.client.get('/api/local-workspaces/local/production/capabilities')
    assert response.status_code == 200
    result = response.json()
    assert result['production_deployment']['status'] == 'not_connected'
    assert result['database_migration']['status'] == 'not_connected'
    assert result['provider_execution_wired'] is False


def test_production_capabilities_hides_adapter_status_errors(adapter):
    production = ProductionAdapters()
    production.capabilities = lambda: (_ for _ in ()).throw(RuntimeError('secret provider detail'))
    adapter.state.production_adapters = production
    response = adapter.client.get('/api/local-workspaces/local/production/capabilities')
    assert response.status_code == 200
    result = response.json()
    for name in ('ci_cd', 'production_deployment', 'database_migration', 'database_restore'):
        assert result[name]['status'] == 'not_connected'
    assert result['provider_execution_wired'] is False
    assert result['automatic_execution'] is False
    assert 'secret provider detail' not in response.text


def test_production_capabilities_rejects_factory_injection(adapter):
    called = []
    adapter.state.production_adapters = lambda: called.append(True) or ProductionAdapters(deployment=lambda proposal: {})
    response = adapter.client.get('/api/local-workspaces/local/production/capabilities')
    assert response.status_code == 200
    result = response.json()
    assert result['production_deployment']['status'] == 'not_connected'
    assert result['provider_execution_wired'] is False
    assert called == []


def test_production_capabilities_map_empty_and_injected_callbacks(adapter):
    adapter.state.production_adapters = ProductionAdapters()
    result = adapter.client.get('/api/local-workspaces/local/production/capabilities').json()
    assert result['production_deployment']['status'] == 'not_connected'
    assert result['database_migration']['status'] == 'not_connected'
    assert result['database_restore']['status'] == 'not_connected'
    assert result['provider_execution_wired'] is False
    assert result['automatic_execution'] is False

    adapter.state.production_adapters = ProductionAdapters(deployment=lambda proposal: {})
    result = adapter.client.get('/api/local-workspaces/local/production/capabilities').json()
    assert result['production_deployment']['status'] == 'configured'
    assert result['database_migration']['status'] == 'not_connected'
    assert result['database_restore']['status'] == 'not_connected'
    assert result['provider_execution_wired'] is True
    assert result['automatic_execution'] is False

    adapter.state.production_adapters = ProductionAdapters(database=lambda proposal: {})
    result = adapter.client.get('/api/local-workspaces/local/production/capabilities').json()
    assert result['production_deployment']['status'] == 'not_connected'
    assert result['database_migration']['status'] == 'configured'
    assert result['database_restore']['status'] == 'configured'
    assert result['provider_execution_wired'] is True
    assert result['automatic_execution'] is False


def configure_service(adapter):
    service = adapter.Service(repository=TrustedRepository(str(adapter.sources["alice"])))
    adapter.state.engineering_operations = service
    return service


def test_confirmation_denial_keeps_exact_approval_for_later_confirmation(adapter):
    service = configure_service(adapter)
    preview = branch_preview(adapter).json()
    url = "/api/local-workspaces/local/git/execute"
    body = {"approval_id": preview["approval_id"]}
    for unconfirmed in (body, {**body, "confirmed": False}):
        response = adapter.client.post(url, json=unconfirmed)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "approval_required"
    assert service.calls == []
    assert service.consume_approval is None
    assert adapter.client.post(url, json={**body, "confirmed": True}).status_code == 200
    assert adapter.client.post(url, json={**body, "confirmed": True}).status_code == 404
    assert service.consume_approval is None
    assert service.confirm_push is None


def test_failed_confirmed_attempt_burns_approval_and_restores_callbacks(adapter):
    service = configure_service(adapter)
    service.fail = True
    preview = branch_preview(adapter).json()
    body = {"approval_id": preview["approval_id"], "confirmed": True}
    url = "/api/local-workspaces/local/git/execute"
    assert adapter.client.post(url, json=body).status_code == 409
    assert adapter.client.post(url, json=body).status_code == 404
    assert len(service.calls) == 1
    assert service.consume_approval is None
    assert service.confirm_push is None


@pytest.mark.parametrize("configuration", ["service", "mapping"])
def test_native_repository_is_scoped_to_exact_owner_source(adapter, configuration):
    if configuration == "service":
        configure_service(adapter)
    else:
        adapter.state.cfg["engineering_operations"] = {"root": str(adapter.sources["alice"])}
    assert branch_preview(adapter).status_code == 200
    assert branch_preview(adapter, "bob").status_code == 403
    capabilities = adapter.client.get(
        "/api/local-workspaces/local/git/capabilities", headers={"x-owner": "bob"},
    ).json()
    assert capabilities["git_branch"]["status"] == "not_connected"
    assert capabilities["local_recovery"]["status"] == "configured"


def test_trusted_repository_attribute_takes_precedence_over_root_cache(adapter):
    service = configure_service(adapter)
    service.repository = TrustedRepository(str(adapter.sources["bob"]))
    assert branch_preview(adapter).status_code == 403


def test_approval_cannot_be_used_by_another_owner(adapter):
    service = configure_service(adapter)
    preview = branch_preview(adapter).json()
    response = adapter.client.post(
        "/api/local-workspaces/local/git/execute", headers={"x-owner": "bob"},
        json={"approval_id": preview["approval_id"], "confirmed": True},
    )
    assert response.status_code == 404
    assert service.calls == []


def test_changed_repository_scope_is_rechecked_before_execution(adapter):
    service = configure_service(adapter)
    preview = branch_preview(adapter).json()
    service.repository = TrustedRepository(str(adapter.sources["bob"]))
    body = {"approval_id": preview["approval_id"], "confirmed": True}
    url = "/api/local-workspaces/local/git/execute"
    assert adapter.client.post(url, json=body).status_code == 403
    assert service.calls == []
    assert adapter.client.post(url, json=body).status_code == 404


def test_root_cache_cannot_override_trusted_repository_scope(adapter):
    service = configure_service(adapter)
    service.root = adapter.sources["bob"]
    assert branch_preview(adapter).status_code == 403


@pytest.mark.parametrize("configured_elsewhere", [False, True])
def test_recovery_available_without_native_repository(adapter, configured_elsewhere):
    if configured_elsewhere:
        adapter.state.cfg["engineering_operations"] = {"root": str(adapter.sources["bob"])}
    capabilities = adapter.client.get("/api/local-workspaces/local/git/capabilities").json()
    assert capabilities["git_branch"]["status"] == "not_connected"
    assert capabilities["git_push"]["status"] == "not_connected"
    assert capabilities["local_recovery"]["status"] == "configured"
    assert branch_preview(adapter).status_code == 403
    preview = adapter.client.post("/api/local-workspaces/local/recovery/proposals", json={})
    assert preview.status_code == 200
    result = adapter.client.post(
        "/api/local-workspaces/local/recovery/execute",
        json={"approval_id": preview.json()["approval_id"], "confirmed": True},
    )
    assert result.status_code == 200
    assert result.json()["action"] == "local_recovery"


@pytest.mark.parametrize('value', [None, '', '   '])
def test_git_proposal_rejects_empty_required_values(adapter, value):
    service = configure_service(adapter)
    def unexpected(*args):
        pytest.fail('Invalid request reached proposal callback')
    service.propose_branch = unexpected
    response = adapter.client.post('/api/local-workspaces/local/git/proposals',
        json={'action': 'git_branch', 'branch': value, 'base_branch': 'main'})
    assert response.status_code == 422


def test_git_proposal_rejects_irrelevant_null_field(adapter):
    configure_service(adapter)
    response = adapter.client.post('/api/local-workspaces/local/git/proposals',
        json={'action': 'git_branch', 'branch': 'feature', 'base_branch': 'main', 'remote': None})
    assert response.status_code == 422


def test_git_and_recovery_approval_cannot_cross_routes(adapter):
    service = configure_service(adapter)
    git = branch_preview(adapter).json()
    recovery = adapter.client.post('/api/local-workspaces/local/recovery/proposals', json={}).json()
    git_body = {'approval_id': git['approval_id'], 'proposal_id': git['proposal_id'], 'confirmed': True}
    recovery_body = {'approval_id': recovery['approval_id'], 'proposal_id': recovery['proposal_id'], 'confirmed': True}
    for route, body in [('recovery', git_body), ('git', recovery_body)]:
        response = adapter.client.post(f'/api/local-workspaces/local/{route}/execute', json=body)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'invalid_proposal'
    assert service.calls == []
    assert adapter.client.post('/api/local-workspaces/local/git/execute', json=git_body).status_code == 200
    assert len(service.calls) == 1
    assert adapter.client.post('/api/local-workspaces/local/recovery/execute', json=recovery_body).status_code == 200


def test_malformed_origin_is_rejected_with_403(adapter):
    configure_service(adapter)
    headers = {'origin': 'http://['}
    assert adapter.client.get('/api/local-workspaces/local/git/capabilities', headers=headers).status_code == 403
    assert adapter.client.post('/api/local-workspaces/local/git/proposals', headers=headers,
                               json={'action': 'git_branch', 'branch': 'feature', 'base_branch': 'main'}).status_code == 403
