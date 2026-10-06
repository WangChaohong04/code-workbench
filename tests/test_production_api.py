"""Production HTTP contracts using only injected in-memory callbacks."""
from types import SimpleNamespace
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from longflow import production_api
from longflow.production_adapters import ProductionAdapters
from longflow.production_api import DeploymentBody, ExecuteBody, MigrationBody, RestoreBody


DIGEST = "a" * 64
CASES = [
    ("deployment", DeploymentBody, {"digest": DIGEST, "target": "prod", "artifact": "image@sha256:abc"}),
    ("database/migration", MigrationBody, {
        "digest": DIGEST, "database_id": "main", "migration": "m1", "backup_id": "b1",
    }),
    ("database/restore", RestoreBody, {
        "digest": DIGEST, "database_id": "main", "backup_id": "b1",
        "recovery_point": "2025-01-01T00:00:00Z", "backup_verification": "verified",
    }),
]


@pytest.mark.parametrize("route,model,values", CASES)
@pytest.mark.parametrize("extra", [{"expires_at": 9999999999}, {"proposal_id": "client"}, {"provider": "url"}])
def test_proposal_schema_rejects_client_control_fields(route, model, values, extra):
    with pytest.raises(ValidationError):
        model(**values, **extra)


@pytest.mark.parametrize("route,model,values", CASES)
def test_proposal_schema_rejects_coerced_strings(route, model, values):
    with pytest.raises(ValidationError):
        model(**{**values, "digest": 123})


@pytest.mark.parametrize("confirmed", [1, "true", None])
def test_confirmation_is_strict(confirmed):
    with pytest.raises(ValidationError):
        ExecuteBody(approval_id="a", proposal_id="p", confirmed=confirmed)


def test_execution_schema_requires_exact_ids_and_defaults_unconfirmed():
    assert ExecuteBody(approval_id="a", proposal_id="p").confirmed is False
    with pytest.raises(ValidationError):
        ExecuteBody(approval_id="a")
    with pytest.raises(ValidationError):
        ExecuteBody(approval_id="a", proposal_id="p", digest=DIGEST)


@pytest.fixture
def api():
    class Connection:
        def execute(self, query, params):
            wid, owner, workspace = params
            row = {"id": wid} if wid in ("local", "other") and owner in ("alice", "bob") and workspace in ("project", "other-project") else None
            return SimpleNamespace(fetchone=lambda: row)

    state = SimpleNamespace(conn=Connection(), cfg={})
    app = FastAPI()

    def principal(request: Request):
        return SimpleNamespace(user=request.headers.get("x-owner", "alice"),
                               workspace=request.headers.get("x-workspace", "project"))

    production_api.install(app, state, principal)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, app=app)


def url(route, action, wid="local"):
    return f"/api/local-workspaces/{wid}/production/{route}/{action}"


def configured(api, *, evidence=True, fail=False, approval=True):
    calls = []
    approvals = []

    def provider(proposal):
        calls.append(proposal)
        if fail:
            raise RuntimeError("transport interrupted")
        return {"status": "completed", "proposal_digest": proposal.digest,
                "provider_evidence": {"run_id": "run-1"} if evidence else {}}

    def approve(proposal):
        approvals.append(proposal)
        return approval

    class RecordingAdapters(ProductionAdapters):
        def __init__(self):
            super().__init__(deployment=provider, database=provider, approval=approve)
            self.proposed = []
            self.arguments = []

        def _register(self, capability, proposal_id, digest, expires_at, payload):
            self.arguments.append((proposal_id, digest, expires_at))
            proposal = super()._register(capability, proposal_id, digest, expires_at, payload)
            self.proposed.append(proposal)
            return proposal

    service = RecordingAdapters()
    api.state.production_adapters = service
    return SimpleNamespace(service=service, calls=calls, approvals=approvals)


def preview(api, route="deployment", values=None):
    return api.client.post(url(route, "proposals"), json=values or CASES[0][2])


def execution(preview):
    return {"approval_id": preview["approval_id"], "proposal_id": preview["proposal_id"], "confirmed": True}


@pytest.mark.parametrize("route,model,values", CASES)
def test_default_closed(api, route, model, values):
    response = preview(api, route, values)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_connected"


@pytest.mark.parametrize("injection", [lambda: ProductionAdapters(), {"deployment": "untrusted"}, object()])
def test_only_adapter_instances_are_trusted(api, injection):
    api.state.production_adapters = injection
    api.state.cfg["production_adapters"] = ProductionAdapters(deployment=lambda proposal: {})
    assert preview(api).status_code == 403


@pytest.mark.parametrize("route,model,values", CASES)
def test_exact_proposal_server_expiry_and_provider_evidence(api, route, model, values):
    recorded = configured(api)
    before = int(time.time())
    response = preview(api, route, values)
    assert response.status_code == 200
    proposal = response.json()
    assert before + 300 <= proposal["expires_at"] <= int(time.time()) + 300
    assert recorded.service.arguments[0][1] == DIGEST
    assert len(recorded.service.arguments[0][0]) == 48
    assert proposal["payload"]["artifact_digest"] == DIGEST
    assert recorded.calls == []
    result = api.client.post(url(route, "execute"), json=execution(proposal))
    assert result.status_code == 200
    assert result.json()["provider_evidence"] == {"run_id": "run-1"}
    assert result.json()["proposal_digest"] == proposal["digest"]
    assert recorded.calls[0] is recorded.service.proposed[0]
    assert recorded.approvals[0] is recorded.calls[0]
    assert api.client.post(url(route, "execute"), json=execution(proposal)).status_code == 404


def test_unconfirmed_does_not_execute_or_consume(api):
    recorded = configured(api)
    proposal = preview(api).json()
    body = execution(proposal)
    for unconfirmed in ({key: value for key, value in body.items() if key != "confirmed"}, {**body, "confirmed": False}):
        result = api.client.post(url("deployment", "execute"), json=unconfirmed)
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "approval_required"
    assert recorded.calls == []
    assert api.client.post(url("deployment", "execute"), json=body).status_code == 200


@pytest.mark.parametrize("headers,wid", [
    ({"x-owner": "bob"}, "local"),
    ({"x-workspace": "other-project"}, "local"),
    ({}, "other"),
])
def test_approval_owner_workspace_and_wid_isolation(api, headers, wid):
    recorded = configured(api)
    proposal = preview(api).json()
    result = api.client.post(url("deployment", "execute", wid), headers=headers, json=execution(proposal))
    assert result.status_code == 404
    assert recorded.calls == []
    assert api.client.post(url("deployment", "execute"), json=execution(proposal)).status_code == 200


@pytest.mark.parametrize('route,model,values', CASES)
def test_expired_http_approval_is_consumed_and_never_calls_provider(api, monkeypatch, route, model, values):
    now = [1000]
    monkeypatch.setattr('longflow.production_api.time.time', lambda: now[0])
    recorded = configured(api)
    response = preview(api, route, values)
    assert response.status_code == 200
    proposal = response.json()
    body = execution(proposal)
    now[0] = proposal['expires_at']
    expired = api.client.post(url(route, 'execute'), json=body)
    assert expired.status_code == 409
    assert expired.json()['detail']['code'] == 'approval_expired'
    assert recorded.calls == []
    assert recorded.approvals == []
    now[0] = 1001
    assert api.client.post(url(route, 'execute'), json=body).status_code == 404


@pytest.mark.parametrize('route,model,values', CASES)
def test_epoch_revocation_consumes_http_approval_before_callbacks(api, route, model, values):
    current_epoch = [7]
    recorded = configured(api)
    recorded.service.epoch = lambda: current_epoch[0]
    response = preview(api, route, values)
    assert response.status_code == 200
    proposal = response.json()
    assert proposal['epoch'] == 7
    body = execution(proposal)
    current_epoch[0] = 8
    revoked = api.client.post(url(route, 'execute'), json=body)
    assert revoked.status_code == 409
    assert revoked.json()['detail']['code'] == 'approval_expired'
    assert recorded.calls == []
    assert recorded.approvals == []
    current_epoch[0] = 7
    assert api.client.post(url(route, 'execute'), json=body).status_code == 404


def test_new_proposal_prunes_expired_http_bindings_but_keeps_live_approvals(api, monkeypatch):
    now = [1000]
    monkeypatch.setattr('longflow.production_api.time.time', lambda: now[0])
    recorded = configured(api)
    expired_response = preview(api)
    assert expired_response.status_code == 200
    expired = expired_response.json()
    now[0] = 1100
    live_response = preview(api)
    assert live_response.status_code == 200
    live = live_response.json()
    now[0] = expired['expires_at']
    assert preview(api).status_code == 200
    assert api.client.post(url('deployment', 'execute'), json=execution(expired)).status_code == 404
    assert recorded.calls == []
    assert recorded.approvals == []
    response = api.client.post(url('deployment', 'execute'), json=execution(live))
    assert response.status_code == 200
    assert len(recorded.calls) == 1
    assert recorded.calls[0].proposal_id == live['proposal_id']


def test_missing_workspace_is_denied(api):
    configured(api)
    assert api.client.post(url("deployment", "proposals", "missing"), json=CASES[0][2]).status_code == 404


def test_mismatched_proposal_and_route_never_execute(api):
    recorded = configured(api)
    proposal = preview(api).json()
    assert api.client.post(url("deployment", "execute"), json={**execution(proposal), "proposal_id": "wrong"}).status_code == 409
    assert api.client.post(url("database/migration", "execute"), json=execution(proposal)).status_code == 409
    assert recorded.calls == []
    assert api.client.post(url("deployment", "execute"), json=execution(proposal)).status_code == 200


@pytest.mark.parametrize("options,code", [
    ({"fail": True}, "in_doubt"),
    ({"evidence": False}, "unverified"),
    ({"approval": False}, "approval_required"),
])
def test_failed_confirmed_attempt_is_consumed(api, options, code):
    recorded = configured(api, **options)
    proposal = preview(api).json()
    body = execution(proposal)
    result = api.client.post(url("deployment", "execute"), json=body)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == code
    assert api.client.post(url("deployment", "execute"), json=body).status_code == 404
    assert len(recorded.calls) == (0 if options.get("approval") is False else 1)


def test_changed_injection_fails_closed_and_consumes(api):
    original = configured(api)
    proposal = preview(api).json()
    configured(api)
    result = api.client.post(url("deployment", "execute"), json=execution(proposal))
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "not_connected"
    assert original.calls == []
    assert api.client.post(url("deployment", "execute"), json=execution(proposal)).status_code == 404


@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"origin": "http://["}, {"sec-fetch-site": "cross-site"}])
def test_cross_site_proposal_and_execute_denied(api, headers):
    recorded = configured(api)
    proposal = preview(api).json()
    assert api.client.post(url("deployment", "proposals"), json=CASES[0][2], headers=headers).status_code == 403
    assert api.client.post(url("deployment", "execute"), json=execution(proposal), headers=headers).status_code == 403
    assert recorded.calls == []
    assert recorded.approvals == []
    assert api.client.post(url("deployment", "execute"), json=execution(proposal)).status_code == 200
    assert len(recorded.calls) == 1


def test_non_loopback_denied(api):
    configured(api)
    with TestClient(api.app, client=("203.0.113.1", 12345)) as remote:
        assert remote.post(url("deployment", "proposals"), json=CASES[0][2]).status_code == 403


def test_invalid_adapter_proposal_maps_to_422(api):
    configured(api)
    result = preview(api, values={**CASES[0][2], "digest": "bad"})
    assert result.status_code == 422
    assert result.json()["detail"]["code"] == "invalid_proposal"
