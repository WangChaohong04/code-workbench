"""Software-engineering entry points. No production executor is installed.

Candidate copies are data, not executable sandboxes. Never run candidate commands
in the service process: tests require a separately isolated runner adapter.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from fastapi import Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db, events, safety
from .code_workspace import CodeWorkspaceService, CodeWorkspaceError


class DevelopmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=16000)
    repository: str = ""
    branch: str = ""


class ContractRequest(BaseModel):
    repository: str = ""
    branch: str = ""
    modules: list[str] = Field(default_factory=list)
    allowed_dirs: list[str] = Field(default_factory=list)
    forbidden_dirs: list[str] = Field(default_factory=list)
    allow_dependencies: bool = False
    allow_config: bool = False
    database: str = "none"
    environment: str = "sandbox"
    acceptance: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    confirmed: bool = False


class CandidateRequest(BaseModel):
    changes: dict[str, str | None] = Field(default_factory=dict)


class FreezeRequest(BaseModel):
    reason: str = "用户安全控制"


def install(app, state, require_principal, scoped_task):
    services = {}
    candidates = {}

    def service(root_id):
        if root_id not in services:
            config = state.cfg.get("safety", {})
            source = config.get("code_source_root")
            base = config.get("code_candidate_base")
            if not source or not base:
                raise HTTPException(409, "sandbox_unavailable: 未配置独立候选副本，禁止原地修改")
            try:
                services[root_id] = CodeWorkspaceService(source, server_owned_base=base)
            except CodeWorkspaceError as exc:
                raise HTTPException(409, {"code": exc.code, "error": str(exc)})
        return services[root_id]

    @app.post("/api/tasks")
    @app.post("/api/development/tasks")
    def create(body: DevelopmentRequest, principal=Depends(require_principal)):
        rid = db.insert_task(state.conn, title=body.goal[:60], kind="goal",
                             agent_role="controller", objective=body.goal, root_id="",
                             status="waiting_user", workspace=principal.workspace,
                             owner=principal.user, slots={"__development__": True})
        contract = safety.ensure_contract(state.conn, rid, body.goal,
                                          owner=principal.user, workspace=principal.workspace)
        events.emit(state.conn, "development_request", task_id=rid, actor=principal.user,
                    detail={"request": body.model_dump(), "note": "代码库与分支为用户提议，尚未核实"})
        db.update_task(state.conn, rid, result={"message": "请确认开发契约；当前仅允许只读分析",
                                              "wait_kind": "development_contract"})
        return {"task": db.get_task(state.conn, rid).to_dict(), "children": [],
                "approvals": [], "contract": contract}

    @app.get("/api/tasks/{task_id}/safety")
    def snapshot(task_id: str, principal=Depends(require_principal)):
        task = scoped_task(task_id, principal)
        result = safety.snapshot(state.conn, task.root_id)
        result["production"] = {"enabled": False, "deployment": "not_connected",
                                "database": "not_connected", "reason": "无受控生产执行器"}
        result["candidates"] = [v for (rid, _), v in candidates.items() if rid == task.root_id]
        return result

    @app.post("/api/tasks/{task_id}/development/contract")
    def confirm(task_id: str, body: ContractRequest, principal=Depends(require_principal)):
        task = scoped_task(task_id, principal)
        if not task.slots.get("__development__"):
            raise HTTPException(409, "仅开发任务支持此契约")
        if safety.is_frozen(state.conn):
            raise HTTPException(409, "冻结期间不能授权写入")
        engineering = body.model_dump(exclude={"confirmed"})
        missing = [k for k in ("repository", "branch", "allowed_dirs", "acceptance", "required_tests")
                   if not engineering[k]]
        for path in body.allowed_dirs + body.forbidden_dirs:
            if (not path or path.startswith("/") or "\\" in path or ":" in path
                    or any(p in ("", ".", "..") for p in path.rstrip("/").split("/"))):
                raise HTTPException(422, "目录须为明确的相对路径，不能包含通配符或路径跳转")
            if any(c in path for c in "*?[]"):
                raise HTTPException(422, "目录不能包含通配符")
        confidence = "high" if body.confirmed and not missing else "medium"
        if body.environment != "sandbox":
            raise HTTPException(403, "正式环境契约不可授权：未接入受控执行器")
        result = safety.confirm_contract(state.conn, task.root_id, actor=principal.user,
                     confidence=confidence, allowed_tools=["code_candidate"] if confidence == "high" else [],
                     scope="sandbox candidate only", constraints=body.constraints, engineering=engineering)
        events.emit(state.conn, "development_contract", task_id=task.root_id, actor=principal.user,
                    detail={"contract": result, "unknown": missing,
                            "reason": "用户确认结构化范围；不授予测试执行、合并或发布权限"})
        return {"contract": result, "unknown": missing}

    @app.post("/api/tasks/{task_id}/candidates")
    def candidate(task_id: str, body: CandidateRequest, principal=Depends(require_principal)):
        task = scoped_task(task_id, principal)
        if safety.is_frozen(state.conn):
            raise HTTPException(409, "global_freeze")
        contract = safety.get_contract(state.conn, task.root_id) or {}
        development = contract.get("engineering", {})
        if (contract.get("confidence") != "high"
                or contract.get("mode") != "write"
                or contract.get("confirmed_by") != principal.user
                or contract.get("confirmed_epoch") != safety.get_epoch(state.conn)
                or "code_candidate" not in contract.get("allowed_tools", [])):
            raise HTTPException(409, "请先确认完整开发契约，禁止猜测写入权限")
        allowed = development.get("allowed_dirs", [])
        forbidden = development.get("forbidden_dirs", [])
        def inside(path, scopes):
            return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in scopes)
        for path in body.changes:
            if not inside(path, allowed) or inside(path, forbidden):
                raise HTTPException(403, "修改超出任务契约")
            name = PurePosixPath(path).name.lower()
            if (name in {"requirements.txt", "package.json", "package-lock.json", "pyproject.toml", "uv.lock"}
                    or name.endswith((".yaml", ".yml", ".toml", ".ini", ".cfg"))
                    or any(x in path.lower() for x in ("auth", "permission", "migration"))
                    or body.changes[path] is None or len(body.changes) > 5):
                raise HTTPException(409, "中高风险候选修改需要精确差异审批；当前适配器保持关闭")
        events.emit(state.conn, "candidate_requested", task_id=task.root_id, actor=principal.user,
                    detail={"resources": list(body.changes), "environment": "sandbox"})
        try:
            result = service(task.root_id).create_candidate(body.changes)
        except CodeWorkspaceError as exc:
            raise HTTPException(409, {"code": exc.code, "error": str(exc)})
        candidates[(task.root_id, result["candidate_id"])] = result
        events.emit(state.conn, "candidate_generated", task_id=task.root_id, actor="code_workspace",
                    detail=result)
        return result

    @app.get("/api/tasks/{task_id}/candidates/{candidate_id}")
    def preview(task_id: str, candidate_id: str, principal=Depends(require_principal)):
        task = scoped_task(task_id, principal)
        if (task.root_id, candidate_id) not in candidates:
            raise HTTPException(404, "候选不存在或服务已重启；需重新生成与审批")
        try:
            return service(task.root_id).preview(candidate_id)
        except CodeWorkspaceError as exc:
            raise HTTPException(409, {"code": exc.code, "error": str(exc)})

    @app.post("/api/tasks/{task_id}/production/{operation}")
    def production(task_id: str, operation: str, principal=Depends(require_principal)):
        task = scoped_task(task_id, principal)
        events.emit(state.conn, "production_denied", task_id=task.root_id, actor=principal.user,
                    detail={"operation": operation, "reason": "controlled_executor_unavailable"})
        raise HTTPException(403, "生产写入保持关闭：未接入可冻结、精确审批及独立验证的执行器")
