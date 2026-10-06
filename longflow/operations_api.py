"""HTTP adapter for explicitly configured engineering operations.

This module is intentionally separate from the local workspace API.  It exposes
preview-only Git proposals and requires an exact, principal-bound approval before
calling :class:`EngineeringOperations`; no repository is discovered from a
request or from LocalWorkspace snapshot storage.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from .engineering_operations import EngineeringOperations, TrustedRepository
from .production_adapters import ProductionAdapters
from .local_workspace import LocalWorkspace
from . import safety


class ProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["git_branch", "git_merge", "git_push"]
    branch: StrictStr | None = None
    base_branch: StrictStr | None = None
    target_branch: StrictStr | None = None
    source_branch: StrictStr | None = None
    remote: StrictStr | None = None
    remote_branch: StrictStr | None = None


class ExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The approval id is issued by this adapter and is never accepted as an
    # owner/workspace identity.  proposal_id is optional for clients that use
    # approval_id as the exact proposal handle; when supplied it must match.
    approval_id: StrictStr
    proposal_id: StrictStr | None = None
    confirmed: StrictBool = False
    confirm_push: StrictBool = False


class RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Binding:
    __slots__ = ("proposal", "service", "owner", "workspace", "wid", "epoch")

    def __init__(self, proposal, service, owner: str, workspace: str, wid: str, epoch: int):
        self.proposal = proposal
        self.service = service
        self.owner = owner
        self.workspace = workspace
        self.wid = wid
        self.epoch = epoch


def install(app, state, require_principal):
    """Install local-workspace Git routes on ``app``.

    Repository configuration is trusted server configuration.  It is read from
    ``state.engineering_operations`` when supplied, otherwise from
    ``state.cfg['engineering_operations']``.  A missing/invalid configuration
    leaves Git unavailable and never falls back to a workspace snapshot.
    """
    conn = state.conn
    lock = threading.RLock()
    pending: dict[str, _Binding] = {}

    def local(request: Request):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "本机工程接口仅允许 loopback 访问")
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlsplit
            try:
                target = urlsplit(origin)
            except ValueError as exc:
                raise HTTPException(403, "跨站请求被拒绝") from exc
            if target.scheme not in ("http", "https") or target.netloc != request.headers.get("host"):
                raise HTTPException(403, "跨站请求被拒绝")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "跨站请求被拒绝")

    def workspace(wid: str, principal):
        row = conn.execute(
            "SELECT * FROM local_workspaces WHERE id=? AND owner=? AND workspace=?",
            (wid, principal.user, principal.workspace),
        ).fetchone()
        if not row:
            raise HTTPException(404, "工作区不存在或无权限")
        return LocalWorkspace(row["source"], row["storage"])

    def require_repository_scope(repository, workspace_obj):
        # Inspect trusted configuration, not a mutable service root cache.
        if (not isinstance(repository, TrustedRepository) or workspace_obj is None
                or not isinstance(repository.root, (str, Path))
                or Path(repository.root) != workspace_obj.source):
            raise HTTPException(403, {"code": "not_connected", "error": "Trusted repository does not match workspace source"})

    def require_service_scope(service, workspace_obj):
        require_repository_scope(service.repository, workspace_obj)
        if service.root != workspace_obj.source:
            raise HTTPException(403, {"code": "not_connected", "error": "Native repository binding changed"})

    def recovery_service(workspace_obj):
        return EngineeringOperations(
            workspace=workspace_obj,
            epoch=lambda: safety.get_epoch(conn),
            check_write=lambda: safety.is_frozen(conn),
        )

    def configured(workspace_obj=None) -> EngineeringOperations:
        existing = getattr(state, "engineering_operations", None)
        if isinstance(existing, EngineeringOperations):
            require_service_scope(existing, workspace_obj)
            return existing
        cfg = getattr(state, "cfg", {}).get("engineering_operations", {}) or {}
        root = cfg.get("root") if isinstance(cfg, dict) else None
        remotes = cfg.get("remote_allowlist", ()) if isinstance(cfg, dict) else ()
        if not root:
            raise HTTPException(403, {"code": "not_connected", "error": "No trusted native repository configured"})
        try:
            repository = TrustedRepository(str(root), tuple(remotes or ()))
            require_repository_scope(repository, workspace_obj)
            return EngineeringOperations(
                repository=repository,
                workspace=workspace_obj,
                epoch=lambda: safety.get_epoch(conn),
                check_write=lambda: safety.is_frozen(conn),
                confirm_push=lambda candidate: False,
            )
        except Exception as exc:
            # Configuration failures are unavailable, never client-controlled errors.
            raise HTTPException(403, {"code": "not_connected", "error": "Trusted native repository unavailable"}) from exc

    def invoke(fn):
        try:
            return fn()
        except HTTPException:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "operation_error")
            raise HTTPException(409, {"code": code, "error": str(exc)}) from exc

    def make_proposal(service, body: ProposalBody):
        supplied = set(body.model_fields_set)
        expected = {
            "git_branch": {"action", "branch", "base_branch"},
            "git_merge": {"action", "target_branch", "source_branch"},
            "git_push": {"action", "branch", "remote", "remote_branch"},
        }[body.action]
        if supplied != expected:
            raise HTTPException(422, "字段必须与操作类型精确匹配")
        for name in expected - {"action"}:
            value = getattr(body, name)
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(422, "操作字段必须为非空字符串")
        if body.action == "git_branch":
            return service.propose_branch(body.branch, body.base_branch)
        if body.action == "git_merge":
            return service.propose_merge(body.target_branch, body.source_branch)
        return service.propose_push(body.branch, body.remote, body.remote_branch)

    def save(proposal, service, principal, wid):
        approval_id = uuid.uuid4().hex
        with lock:
            pending[approval_id] = _Binding(
                proposal, service, principal.user, principal.workspace, wid,
                proposal.epoch,
            )
        return approval_id

    def get_binding(body, wid, principal):
        with lock:
            binding = pending.get(body.approval_id)
        if binding is None or binding.wid != wid or binding.owner != principal.user or binding.workspace != principal.workspace:
            raise HTTPException(404, "审批不存在或无权限")
        if body.proposal_id is not None and body.proposal_id != binding.proposal.proposal_id:
            raise HTTPException(409, "审批与提案不匹配")
        return binding

    def execute(wid, body, principal, request, allowed_actions):
        local(request)
        if body.confirmed is not True:
            raise HTTPException(409, {"code": "approval_required", "error": "Explicit confirmation required"})
        with lock:
            binding = get_binding(body, wid, principal)
            proposal = binding.proposal
            if proposal.action not in allowed_actions:
                raise HTTPException(409, {"code": "invalid_proposal", "error": "Approval does not belong to this route"})
            service = binding.service
            consume_approval, confirm_push = service.consume_approval, service.confirm_push
            try:
                if proposal.action != "local_recovery":
                    require_service_scope(service, workspace(wid, principal))
                # Install and use request-bound callbacks in the same critical section.
                service.consume_approval = lambda candidate: candidate is proposal
                service.confirm_push = lambda candidate: candidate is proposal and body.confirm_push
                return invoke(lambda: service.execute(proposal))
            finally:
                service.consume_approval, service.confirm_push = consume_approval, confirm_push
                pending.pop(body.approval_id, None)

    @app.get("/api/local-workspaces/{wid}/production/capabilities")
    def production_capabilities(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        workspace(wid, principal)
        adapter = getattr(state, 'production_adapters', None)
        if not isinstance(adapter, ProductionAdapters):
            return {
                "ci_cd": {"status": "not_connected"},
                "production_deployment": {"status": "not_connected"},
                "database_migration": {"status": "not_connected"},
                "database_restore": {"status": "not_connected"},
                "provider_execution_wired": False, "approval_required": True,
                "automatic_execution": False,
            }
        try:
            caps = adapter.capabilities()
            if not isinstance(caps, dict):
                caps = {}
        except Exception:
            caps = {}
        def capability(name):
            value = caps.get(name)
            allowed_statuses = {"configured", "not_connected", "blocked"}
            status = value.get("status") if isinstance(value, dict) else None
            if isinstance(status, str) and status in allowed_statuses:
                return {"status": status}
            return {"status": "not_connected"}
        wired = callable(adapter.deployment) or callable(adapter.database)
        return {"ci_cd": capability("ci_cd"),
                "production_deployment": capability("production_deployment"),
                "database_migration": capability("production_database"),
                "database_restore": capability("production_restore"),
                "provider_execution_wired": wired, "approval_required": True,
                "automatic_execution": False}

    @app.get("/api/local-workspaces/{wid}/git/capabilities")
    def git_capabilities(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        s = workspace(wid, principal)
        capabilities = recovery_service(s).capabilities()
        with lock:
            try:
                git = configured(s).capabilities()
                for action in ("git_branch", "git_merge", "git_push"):
                    capabilities[action] = git[action]
            except HTTPException:
                pass
        return capabilities

    @app.post("/api/local-workspaces/{wid}/git/proposals")
    def git_proposal(wid: str, body: ProposalBody, request: Request, principal=Depends(require_principal)):
        local(request)
        s = workspace(wid, principal)
        with lock:
            service = configured(s)
            proposal = invoke(lambda: make_proposal(service, body))
            approval_id = save(proposal, service, principal, wid)
        return {**proposal.to_dict(), "approval_id": approval_id}

    @app.post("/api/local-workspaces/{wid}/git/execute")
    def git_execute(wid: str, body: ExecuteBody, request: Request, principal=Depends(require_principal)):
        return execute(wid, body, principal, request, {"git_branch", "git_merge", "git_push"})

    @app.post("/api/local-workspaces/{wid}/recovery/proposals")
    def recovery_proposal(wid: str, body: RecoveryBody, request: Request, principal=Depends(require_principal)):
        local(request)
        s = workspace(wid, principal)
        service = recovery_service(s)
        proposal = invoke(lambda: service.propose_recovery())
        approval_id = save(proposal, service, principal, wid)
        return {**proposal.to_dict(), "approval_id": approval_id}

    @app.post("/api/local-workspaces/{wid}/recovery/execute")
    def recovery_execute(wid: str, body: ExecuteBody, request: Request, principal=Depends(require_principal)):
        return execute(wid, body, principal, request, {"local_recovery"})
