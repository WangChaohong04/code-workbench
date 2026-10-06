"""FastAPI 应用：REST API + 静态工作台。"""
from __future__ import annotations

import json
import time
import threading
import re
from pathlib import Path
from typing import Any, Literal

import httpx
from contextlib import asynccontextmanager
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from .auth import Authenticator, Principal, AuthError, principal_dependency
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


from . import config as cfg_mod
from . import db, events, orchestrator, safety
from .models import Task
from .tools import ToolRegistry, ToolRuntime, register_core_tools
from . import development
from .workers import RecoveryWorker

WEB_DIR = Path(__file__).resolve().parent / "web"


# ---- 轻量请求限流（进程内滑动窗口；按 key 隔离，秒级互斥） ----
_RATE_WINDOWS: dict = {}
_RATE_LOCK = threading.Lock()


def _rate_limited(key: str, *, limit: int, window_seconds: float) -> None:
    """滑动窗口限流；超限抛 AuthError(429)，由全局异常处理器转 JSON。"""
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE_WINDOWS.setdefault(key, [])
        while q and now - q[0] > window_seconds:
            q.pop(0)
        if len(q) >= int(limit):
            raise AuthError("rate_limited", "请求过于频繁，请稍后再试", status=429)
        q.append(now)



class Message(BaseModel):
    text: str
    slots: dict[str, Any] | None = None


class Decision(BaseModel):
    decision: str  # approved | rejected


class Grant(BaseModel):
    scope: str
    tool_name: str | None = None
    object_pattern: str | None = None
    max_count: int | None = None

class EngineeringContract(BaseModel):
    """Human-reviewed scope metadata, not executable filesystem permissions."""
    model_config = ConfigDict(extra="forbid")

    repository: StrictStr = ""
    branch: StrictStr = ""
    modules: list[StrictStr] = Field(default_factory=list)
    allowed_dirs: list[StrictStr] = Field(default_factory=list)
    forbidden_dirs: list[StrictStr] = Field(default_factory=list)
    allow_dependencies: StrictBool = False
    allow_config: StrictBool = False
    database: StrictStr = ""
    environment: StrictStr = ""
    acceptance: list[StrictStr] = Field(default_factory=list)
    required_tests: list[StrictStr] = Field(default_factory=list)
    constraints: list[StrictStr] = Field(default_factory=list)
    stop_conditions: list[StrictStr] = Field(default_factory=list)


class ConfirmContract(BaseModel):
    # Never accept actor/owner/workspace/role from the client.
    model_config = ConfigDict(extra="forbid")

    allowed_tools: list[StrictStr] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"
    scope: StrictStr = ""
    constraints: list[StrictStr] = Field(default_factory=list)
    resources: dict[str, list[StrictStr]] = Field(default_factory=dict)
    denied_tools: list[StrictStr] = Field(default_factory=list)
    engineering: EngineeringContract | None = None


class SafetyControlAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = ""


class AppState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.conn = db.connect(cfg["db_path"])
        db.init_db(self.conn)
        self.authenticator = Authenticator(cfg)
        self.worker = None
        self.registry = ToolRegistry()
        register_core_tools(self.registry)
        http_client = None
        try:
            http_client = httpx.Client(timeout=20)
        except Exception:  # noqa: BLE001
            http_client = None
        self.runtime = ToolRuntime(
            self.conn, self.registry,
            timeout_seconds=cfg["limits"].get("tool_timeout_seconds", 30),
            http=http_client,
        )

    def engine(self) -> orchestrator.Engine:
        # 请求线程使用 AppState 连接（SQLite WAL + check_same_thread=False）。
        return orchestrator.Engine(self.conn, self.cfg, self.runtime)

    def worker_engine(self) -> orchestrator.Engine:
        # 后台 worker 在独立线程：新建独立连接与独立 ToolRuntime，
        # 使编排读写、工具执行、事件落库都走 worker 连接，不跨线程共享请求连接。
        conn = db.connect(self.cfg["db_path"])
        runtime = ToolRuntime(
            conn, self.registry,
            timeout_seconds=self.cfg["limits"].get("tool_timeout_seconds", 30),
            http=self.runtime.http,
        )
        return orchestrator.Engine(conn, self.cfg, runtime)


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or cfg_mod.load_config()
    state = AppState(cfg)
    require_principal = principal_dependency(state.authenticator)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 后台恢复循环：进程启动即恢复 in-flight 任务；关闭时停止。
        _lim = cfg.get("limits", {})
        interval = float(_lim.get("recovery_interval_seconds",
                             _lim.get("tick_interval_seconds", 1.0)))
        state.worker = RecoveryWorker(
            state.worker_engine, interval_seconds=interval,
            max_parallel=int(_lim.get("worker_concurrency", 4)))
        state.worker.start()
        try:
            yield
        finally:
            if state.worker:
                state.worker.stop()
                state.worker = None

    app = FastAPI(title="AI 软件工程安全工作台", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(AuthError)
    async def _auth_err(request, exc: AuthError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.message, "code": exc.code})
    app.state.lf = state
    def _task_detail(task: Task) -> dict:
        children = [c.to_dict() for c in db.list_children(state.conn, task.root_id)
                    if c.id != task.id]
        approvals = [
            dict(r) for r in state.conn.execute(
                "SELECT * FROM approvals WHERE task_id IN (SELECT id FROM tasks WHERE root_id=?)",
                (task.root_id,),
            ).fetchall()
        ]
        for a in approvals:
            a["args"] = json.loads(a.pop("args_json"))
            a.pop("args_hash", None)
        return {
            "task": task.to_dict(),
            "children": children,
            "approvals": approvals,
            "events": events.list_events(state.conn, task.id),
            "safety": safety.snapshot(state.conn, task.root_id),
        }

    @app.get("/api/health")
    def health():
        # 有效驱动以运行时实例为准：配置 openai_compatible 但缺 key/不可达会回退本地。
        try:
            eff = state.engine().driver
        except Exception:  # noqa: BLE001
            eff = None
        from . import model_ledger as ml
        try:
            ml.init_ledger(state.conn)
            calls = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls").fetchone()["n"]
            fb = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE fallback=1").fetchone()["n"]
            fails = state.conn.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE ok=0").fetchone()["n"]
        except Exception:  # noqa: BLE001
            calls = fb = fails = 0
        configured = cfg["llm"].get("driver", "local")
        return {
            "ok": True, "version": "0.1.0",
            "configured_driver": configured,
            "effective_driver": getattr(eff, "name", "unknown"),
            "degraded_to_local": getattr(eff, "name", None) == "local" and configured != "local",
            "llm_calls": calls, "llm_fallbacks": fb, "llm_failures": fails,
            "auth_mode": "local_single_user" if state.authenticator.local_dev else "token",
        }

    @app.get("/api/config")
    def get_config():
        out = cfg_mod.public_config(cfg)
        out["tools"] = state.registry.names()
        return out

    def _scoped_task(task_id: str, principal: Principal):
        task = db.get_task(state.conn, task_id)
        if task is None:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        row = state.conn.execute("SELECT workspace, owner FROM tasks WHERE id=?",
                                 (task.root_id,)).fetchone()
        if not principal.is_admin and row["workspace"] != principal.workspace:
            raise HTTPException(404, {"error": "任务不存在", "code": "not_found"})
        if not principal.is_admin and row["owner"] != principal.user:
            raise HTTPException(403, {"error": "无权访问该任务", "code": "forbidden"})
        return task

    # Software-engineering routes use the same authenticated task scoping helper.
    development.install(app, state, require_principal, _scoped_task)

    @app.get("/api/tasks")
    def list_tasks(principal: Principal = Depends(require_principal)):
        if principal.is_admin:
            roots = db.list_tasks(state.conn, root_only=True)
        else:
            roots = db.list_roots(state.conn, workspace=principal.workspace)
            roots = [t for t in roots if _owner(state, t) == principal.user]
        return {"tasks": [t.to_dict() for t in roots]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        target = db.get_task(state.conn, task.root_id)
        return _task_detail(target)

    @app.get("/api/tasks/{task_id}/contract")
    def get_contract(task_id: str, principal: Principal = Depends(require_principal)):
        from . import safety
        task = _scoped_task(task_id, principal)
        contract = safety.get_contract(state.conn, task.root_id)
        if contract is None:
            raise HTTPException(404, {"error": "Contract not found", "code": "not_found"})
        return contract

    @app.post("/api/tasks/{task_id}/contract")
    def confirm_contract(task_id: str, body: ConfirmContract,
                         principal: Principal = Depends(require_principal)):
        from . import safety
        from .permissions import PermissionDenied
        task = _scoped_task(task_id, principal)
        root = db.get_task(state.conn, task.root_id)
        row = state.conn.execute("SELECT owner,workspace FROM tasks WHERE id=?",
                                 (task.root_id,)).fetchone()
        # Admin visibility is not authority to confirm another user's contract.
        if row["owner"] != principal.user:
            raise HTTPException(403, {"error": "Only the owner may confirm the contract",
                                      "code": "forbidden"})
        if "code_candidate" in body.allowed_tools:
            # Candidate authorization requires the dedicated development route's
            # complete scope, sandbox and path validation, not this generic form.
            raise HTTPException(403, {"error": "Use the development contract endpoint",
                                      "code": "development_contract_required"})
        try:
            safety.ensure_contract(state.conn, root.id, root.objective,
                                   owner=row["owner"], workspace=row["workspace"])
            return safety.confirm_contract(
                state.conn, root.id, actor=principal.user, actor_role="user",
                allowed_tools=body.allowed_tools, confidence=body.confidence,
                scope=body.scope, constraints=body.constraints, resources=body.resources,
                denied_tools=body.denied_tools,
                engineering=body.engineering.model_dump() if body.engineering is not None else None)
        except PermissionDenied as exc:
            raise HTTPException(403, {"error": str(exc), "code": "safety_denied"})
        except ValueError as exc:
            raise HTTPException(422, {"error": str(exc), "code": "invalid_contract"})

    @app.get("/api/tasks/{task_id}/contract/snapshot")
    def contract_snapshot(task_id: str, principal: Principal = Depends(require_principal)):
        from . import safety
        task = _scoped_task(task_id, principal)
        return safety.snapshot(state.conn, task.root_id)

    @app.get("/api/tasks/{task_id}/verification")
    def verification_status(task_id: str, principal: Principal = Depends(require_principal)):
        """Read trusted status records; clients cannot self-certify verification."""
        from . import safety
        task = _scoped_task(task_id, principal)
        view = safety.snapshot(state.conn, task.root_id)
        progress = view["progress"] or {}
        return {"root_id": task.root_id,
                "execution_status": progress.get("execution_status", "not_started"),
                "verification_status": progress.get("verification_status", "not_started"),
                "progress": view["progress"], "boundary": view["boundary"]}

    @app.post("/api/safety/freeze")
    def freeze(body: SafetyControlAction = Body(default=SafetyControlAction()),
               principal: Principal = Depends(require_principal)):
        from . import safety
        state.authenticator.require_admin(principal)
        return safety.freeze(state.conn, actor=principal.user, actor_role="user",
                             reason=body.reason)

    @app.post("/api/safety/unfreeze")
    def unfreeze(body: SafetyControlAction = Body(default=SafetyControlAction()),
                 principal: Principal = Depends(require_principal)):
        from . import safety
        state.authenticator.require_admin(principal)
        # Epoch changes intentionally do not restore stale contracts or approvals.
        return safety.unfreeze(state.conn, actor=principal.user, actor_role="user",
                               reason=body.reason)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        engine = state.engine()
        return engine.cancel(task.root_id)

    @app.post("/api/tasks/{task_id}/message")
    def post_message(task_id: str, body: Message = Body(...),
                     principal: Principal = Depends(require_principal)):
        task = _scoped_task(task_id, principal)
        engine = state.engine()
        engine.user_message(task.root_id, body.text, body.slots)
        return _task_detail(db.get_task(state.conn, task.root_id))

    @app.post("/api/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: Decision = Body(...),
               principal: Principal = Depends(require_principal)):
        row = state.conn.execute(
            "SELECT a.task_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
            (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(404, {"error": "审批不存在", "code": "not_found"})
        _scoped_task(row["task_id"], principal)
        engine = state.engine()
        try:
            return engine.decide_approval(approval_id, body.decision, by=principal.user)
        except FileNotFoundError:
            raise HTTPException(404, {"error": "审批不存在", "code": "not_found"})


    @app.post("/api/grants")
    def add_grant(body: Grant = Body(...), principal: Principal = Depends(require_principal)):
        state.authenticator.require_admin(principal)  # 仅管理员/授权角色
        from .permissions import add_grant as _add
        gid = _add(state.conn, scope=body.scope, tool_name=body.tool_name,
                   object_pattern=body.object_pattern, max_count=body.max_count)
        return {"grant_id": gid}

    def _owner(state_, task_obj):
        r = state_.conn.execute("SELECT owner FROM tasks WHERE id=?", (task_obj.root_id,)).fetchone()
        return r["owner"] if r else None

    from . import local_workspace_api, operations_api, production_api
    local_workspace_api.install(app, state, require_principal)
    operations_api.install(app, state, require_principal)
    production_api.install(app, state, require_principal)

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app

