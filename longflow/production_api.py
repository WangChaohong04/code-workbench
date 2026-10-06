"""Explicit, principal-bound HTTP approvals for trusted production adapters.

No provider is configured or called automatically. Only a server-injected
ProductionAdapters instance can propose or execute a production operation.
"""
from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from .production_adapters import ProductionAdapters, ProductionCapabilityError, ProductionProposal


class DeploymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: StrictStr
    target: StrictStr
    artifact: StrictStr


class MigrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: StrictStr
    database_id: StrictStr
    migration: StrictStr
    backup_id: StrictStr


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: StrictStr
    database_id: StrictStr
    backup_id: StrictStr
    recovery_point: StrictStr
    backup_verification: StrictStr


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: StrictStr
    proposal_id: StrictStr
    confirmed: StrictBool = False


@dataclass(frozen=True)
class _Binding:
    proposal: ProductionProposal
    service: ProductionAdapters
    wid: str
    owner: str
    workspace: str


def install(app, state, require_principal):
    """Install six explicit production routes, closed without trusted injection."""
    pending: dict[str, _Binding] = {}
    lock = threading.RLock()

    def local(request):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "Production interface requires loopback access")
        origin = request.headers.get("origin")
        if origin:
            try:
                target = urlsplit(origin)
            except ValueError as exc:
                raise HTTPException(403, "Cross-site request denied") from exc
            if target.scheme not in ("http", "https") or target.netloc != request.headers.get("host"):
                raise HTTPException(403, "Cross-site request denied")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "Cross-site request denied")

    def workspace(wid, principal):
        row = state.conn.execute(
            "SELECT * FROM local_workspaces WHERE id=? AND owner=? AND workspace=?",
            (wid, principal.user, principal.workspace),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Workspace not found or unauthorized")

    def configured():
        service = getattr(state, "production_adapters", None)
        if not isinstance(service, ProductionAdapters):
            raise HTTPException(403, {"code": "not_connected", "error": "No trusted production adapter configured"})
        return service

    def invoke(fn, *, proposing=False):
        try:
            return fn()
        except ProductionCapabilityError as exc:
            status = 403 if exc.code == "not_connected" else 409
            if proposing and exc.code == "invalid_proposal":
                status = 422
            raise HTTPException(status, {"code": exc.code, "error": str(exc)}) from exc
        except Exception as exc:
            # Callback/configuration internals must not become response evidence.
            raise HTTPException(409, {"code": "operation_error", "error": "Production operation failed"}) from exc

    def propose(wid, body, request, principal, method):
        local(request)
        workspace(wid, principal)
        service = configured()
        proposal = invoke(lambda: getattr(service, method)(
            proposal_id=secrets.token_hex(24),
            expires_at=int(time.time()) + 300,
            **body.model_dump(),
        ), proposing=True)
        approval_id = secrets.token_hex(24)
        with lock:
            now = time.time()
            pending_keys = [key for key, value in pending.items() if value.proposal.expires_at <= now]
            for key in pending_keys:
                pending.pop(key, None)
            pending[approval_id] = _Binding(proposal, service, wid, principal.user, principal.workspace)
        return {
            "approval_id": approval_id,
            "proposal_id": proposal.proposal_id,
            "capability": proposal.capability,
            "digest": proposal.digest,
            "epoch": proposal.epoch,
            "expires_at": proposal.expires_at,
            "payload": dict(proposal.payload),
        }

    def execute(wid, body, request, principal, capability):
        local(request)
        workspace(wid, principal)
        if body.confirmed is not True:
            raise HTTPException(409, {"code": "approval_required", "error": "Explicit confirmation required"})
        with lock:
            binding = pending.get(body.approval_id)
            if (binding is None or binding.wid != wid or binding.owner != principal.user
                    or binding.workspace != principal.workspace):
                raise HTTPException(404, "Approval not found or unauthorized")
            if (body.proposal_id != binding.proposal.proposal_id
                    or binding.proposal.capability != capability):
                raise HTTPException(409, {"code": "invalid_proposal", "error": "Approval and proposal do not match"})
            # Burn before any trusted callbacks: ambiguous provider failures cannot replay.
            del pending[body.approval_id]
        service = configured()
        if service is not binding.service:
            raise HTTPException(403, {"code": "not_connected", "error": "Trusted production adapter changed"})
        return invoke(lambda: service.execute(binding.proposal))

    @app.post("/api/local-workspaces/{wid}/production/deployment/proposals")
    def deployment_proposal(wid: str, body: DeploymentBody, request: Request, principal=Depends(require_principal)):
        return propose(wid, body, request, principal, "propose_deployment")

    @app.post("/api/local-workspaces/{wid}/production/deployment/execute")
    def deployment_execute(wid: str, body: ExecuteBody, request: Request, principal=Depends(require_principal)):
        return execute(wid, body, request, principal, "production_deployment")

    @app.post("/api/local-workspaces/{wid}/production/database/migration/proposals")
    def migration_proposal(wid: str, body: MigrationBody, request: Request, principal=Depends(require_principal)):
        return propose(wid, body, request, principal, "propose_database")

    @app.post("/api/local-workspaces/{wid}/production/database/migration/execute")
    def migration_execute(wid: str, body: ExecuteBody, request: Request, principal=Depends(require_principal)):
        return execute(wid, body, request, principal, "production_database")

    @app.post("/api/local-workspaces/{wid}/production/database/restore/proposals")
    def restore_proposal(wid: str, body: RestoreBody, request: Request, principal=Depends(require_principal)):
        return propose(wid, body, request, principal, "propose_restore")

    @app.post("/api/local-workspaces/{wid}/production/database/restore/execute")
    def restore_execute(wid: str, body: ExecuteBody, request: Request, principal=Depends(require_principal)):
        return execute(wid, body, request, principal, "production_restore")
