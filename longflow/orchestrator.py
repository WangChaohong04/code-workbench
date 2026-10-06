"""主控 Agent 编排：入口闸门、任务图调度、权限工具调用、出口闸门。

设计原则：
- 模型提出下一步动作，Harness 验证并执行（权限/审批/幂等/预算/日志）。
- 任务独立于单次模型调用存在，状态持久化；run_root/tick 可重入，重启后继续。
- 一个分支等待审批不阻塞其他独立分支。
"""
from __future__ import annotations

import json
import re
import threading
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from . import config as cfg_mod
from . import db, events, llm, permissions, rag, verification, safety
from . import subagents as sa_mod
from .subagent_runner import SubagentRunner
from . import model_ledger as mledger
from .models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    READY,
    TERMINAL_STATUSES,
    WAITING_APPROVAL,
    WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL,
    PARTIALLY_COMPLETED, RETRYING,
    ROLE_CONTROLLER,
    ROLE_EXECUTOR,
    ROLE_RESEARCHER,
    ROLE_VERIFIER,
)
from .tools import ToolRuntime, ToolResultInDoubt

# 同一数据库文件下按 root 序列化 tick：请求线程与后台 RecoveryWorker 不并发跑同一 root。
_TICK_LOCKS: dict = {}
_TICK_LOCKS_GUARD = threading.Lock()


def _root_lock(db_path: str, root_id: str) -> "threading.RLock":
    key = f"{db_path}:{root_id}"
    with _TICK_LOCKS_GUARD:
        lk = _TICK_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _TICK_LOCKS[key] = lk
        return lk


EMOTION_HINTS = ["非常生气", "愤怒", "投诉", "差评"]

# 多轮消息分类（确定性、场景无关的关键词信号）
_NEW_TASK_HINTS = ["重新开始", "新任务", "另一件事", "换个问题", "新的问题", "帮我做另"]
_CONSTRAINT_CHANGE_HINTS = ["改成", "改为", "换成", "预算改成", "数量改", "调整为", "不是", "应该是",
                            "预算变", "改为预算", "预算提高", "预算降低"]


def classify_followup(text: str, *, is_waiting_clarify: bool, slot_updates: dict | None = None) -> str:
    """把一条用户消息归入多轮语义类别（供编排/记录，不做复杂 NLP）：

    - missing_info: 任务正等待澄清，消息在补缺失槽位；
    - constraint_change: 已给出的约束/槽位发生变化（需重新核验/重规划）；
    - new_task: 明确要另起任务；
    - follow_up: 其余追问/补充（沿用上下文）。
    """
    t = (text or "").strip()
    if any(h in t for h in _NEW_TASK_HINTS):
        return "new_task"
    if is_waiting_clarify:
        return "missing_info"
    # 显式槽位变更（与现有槽位同名且值不同）视为约束变更
    if slot_updates:
        return "constraint_change"
    if any(h in t for h in _CONSTRAINT_CHANGE_HINTS):
        return "constraint_change"
    return "follow_up"


class TaskGraphError(Exception):
    pass


def _interp(value, slots: dict, goal: str = ""):
    """{slots.xxx} / {goal} 占位插值；空值（None/""）的过滤条件自动剔除。"""
    if isinstance(value, str):
        def repl(m):
            token = m.group(1)
            if token == "goal":
                return goal
            v = slots.get(token[len("slots."):])
            return "" if v is None else str(v)
        return re.sub(r"\{(slots\.[a-zA-Z0-9_]+|goal)\}", repl, value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            iv = _interp(v, slots, goal)
            if iv not in (None, ""):
                out[k] = iv
        return out
    if isinstance(value, list):
        return [_interp(v, slots, goal) for v in value]
    return value


def _coerce_numbers(args: dict) -> dict:
    """把数值型字符串参数还原为数字；剔除空字典/空字符串参数（模板插值后处理）。"""
    out = {}
    for k, v in (args or {}).items():
        if v is None:
            continue
        if isinstance(v, dict):
            if v:  # 空字典（如 filters: {}）不下发；非空（如 filters.category=咖啡馆）保留
                cleaned = {kk: _coerce_numbers({kk: vv})[kk] for kk, vv in v.items()
                           if vv not in (None, "")}
                if cleaned:
                    out[k] = cleaned
            continue
        if isinstance(v, str):
            if v == "":
                continue
            m = re.fullmatch(r"-?\d+", v)
            if m:
                out[k] = int(v)
                continue
            m = re.fullmatch(r"-?\d+\.\d+", v)
            if m:
                out[k] = float(v)
                continue
        out[k] = v
    return out


# 可恢复瞬时错误（网络/超时/外部源暂时不可达）：允许有界自动重试；
# source_not_configured 是配置缺口（非瞬时），不重试。
_RETRIABLE_ERRORS = ("timeout", "timed out", "search_unreachable", "source_unreachable",
                     "connection", "temporar", "502", "503", "504", "reset",
                     "subagent 执行异常")


def _is_retriable_error(error: str | None) -> bool:
    e = (error or "").lower()
    return any(k in e for k in _RETRIABLE_ERRORS)


class Engine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: dict,
        runtime: ToolRuntime,
        driver: llm.BaseDriver | None = None,
    ):
        self.conn = conn
        self.cfg = cfg
        self.runtime = runtime
        self.driver = driver or llm.build_driver(cfg)
        self.limits = cfg.get("limits", {})
        try:
            mledger.init_ledger(self.conn)
        except Exception:  # noqa: BLE001
            pass
        self.budget = mledger.ModelBudget(self.conn, self.limits.get("max_llm_calls"))

    def _record_driver_reports(self, task) -> None:
        """抽取驱动真实 HTTP 调用记录（usage/token/重试/降级/错误）写入模型账本。

        以驱动边界的真实请求为准（含重试尝试与降级标记），模板/规则动作不在此列。
        token 不可得时保持 None，不写 0。
        """
        drain = getattr(self.driver, "drain_reports", None)
        if not callable(drain):
            return
        for rec in drain() or []:
            try:
                self.budget.check(task.root_id)
            except Exception:
                pass
            mledger.record_call(
                self.conn,
                driver=rec.get("driver", "unknown"),
                model=rec.get("model"),
                call_type=rec.get("call_type", "chat"),
                ok=bool(rec.get("ok")),
                fallback=bool(rec.get("fallback")),
                latency_ms=rec.get("latency_ms"),
                prompt_tokens=rec.get("prompt_tokens"),
                completion_tokens=rec.get("completion_tokens"),
                error=rec.get("error"),
                task_id=task.id, root_id=task.root_id,
            )
            # 降级/失败同时发可见事件（不再静默）
            if rec.get("fallback") or not rec.get("ok"):
                events.emit(
                    self.conn, events.LLM_CALL, task_id=task.id,
                    actor=f"agent:{task.agent_role}",
                    detail={"driver": rec.get("driver"), "model": rec.get("model"),
                            "real_model_call": True, "fallback": bool(rec.get("fallback")),
                            "ok": bool(rec.get("ok")),
                            "error": rec.get("error"),
                            "call_type": rec.get("call_type")},
                )

    # ---------- 创建任务与入口闸门 ----------

    def create_goal(self, goal_text: str, scenario: str = "engineering", slots: dict | None = None,
                    *, workspace: str = "default", owner: str = "local") -> dict:
        """Compatibility entry: create an engineering contract, never a scenario workflow."""
        if scenario != "engineering":
            raise ValueError("Only engineering goals are supported")
        root_id = db.insert_task(
            self.conn, title=goal_text[:60], kind="goal", agent_role=ROLE_CONTROLLER,
            objective=goal_text, root_id="", status=WAITING_USER,
            slots={"__development__": True, "__scenario__": "engineering"},
            workspace=workspace, owner=owner)
        self.conn.execute("UPDATE tasks SET root_id=? WHERE id=?", (root_id, root_id))
        self.conn.commit()
        safety.ensure_contract(self.conn, root_id, goal_text, owner=owner, workspace=workspace)
        events.emit(self.conn, events.TASK_CREATED, task_id=root_id, actor=owner,
                    detail={"goal": goal_text, "scenario": "engineering"})
        return {"task_id": root_id, "verdict": "clarify"}

    def _root_ws(self, root_id: str, default="default") -> str:
        r = self.conn.execute("SELECT workspace FROM tasks WHERE id=?", (root_id,)).fetchone()
        return r["workspace"] if r else default

    def _root_owner(self, root_id: str, default="local") -> str:
        r = self.conn.execute("SELECT owner FROM tasks WHERE id=?", (root_id,)).fetchone()
        return r["owner"] if r else default

    def _materialize_plan(self, root_id: str, plan: dict) -> None:
        root = db.get_task(self.conn, root_id)
        templates = plan.get("tasks", [])
        if not templates:
            raise TaskGraphError("An explicit engineering task graph is required")
        id_by_idx: dict[int, str] = {}
        for idx, tpl in enumerate(templates):
            tid = db.insert_task(
                self.conn,
                title=(tpl.get("title") or tpl.get("objective", f"子任务{idx+1}"))[:60],
                kind="subtask",
                agent_role=tpl.get("role", ROLE_RESEARCHER),
                objective=tpl.get("objective", ""),
                root_id=root_id,
                parent_id=root_id,
                depends_on=[],
                slots=dict(root.slots),
                status=PENDING,
                workspace=self._root_ws(root_id, "default"),
                owner=self._root_owner(root_id, "local"),
            )
            id_by_idx[idx] = tid
            self.conn.execute(
                "UPDATE tasks SET plan_json=? WHERE id=?",
                (json.dumps({
                    "subagent": tpl.get("subagent"),
                    "request": tpl.get("request") or {},
                    "tool_hint": tpl.get("tool"),
                    "tool_args": tpl.get("tool_args"),
                    "risk": tpl.get("risk", "low"),
                    "side_effect": tpl.get("side_effect", False),
                    "template_id": tpl.get("id"),
                    "template_index": idx,
                }, ensure_ascii=False), tid),
            )
        dep_pairs = []
        for idx, tpl in enumerate(templates):
            for dep_idx in tpl.get("depends", []) or []:
                if dep_idx not in id_by_idx:
                    raise TaskGraphError(f"子任务 {idx} 依赖不存在的任务索引 {dep_idx}")
                dep_pairs.append((id_by_idx[idx], id_by_idx[dep_idx]))
        self._check_cycle(dep_pairs)
        for tid, dep_id in dep_pairs:
            task = db.get_task(self.conn, tid)
            deps = task.depends_on + [dep_id]
            self.conn.execute("UPDATE tasks SET depends_on_json=? WHERE id=?",
                              (json.dumps(deps, ensure_ascii=False), tid))
        self.conn.commit()
        db.update_task(self.conn, root_id, status=IN_PROGRESS,
                       plan={"intent": plan.get("intent"), "children": list(id_by_idx.values())})
        events.emit(self.conn, events.TASK_STATUS, task_id=root_id, actor="controller",
                    detail={"status": IN_PROGRESS, "children": len(id_by_idx)})

    @staticmethod
    def _check_cycle(pairs: list[tuple[str, str]]) -> None:
        adj: dict[str, list[str]] = {}
        nodes = set()
        for a, b in pairs:  # a 依赖 b
            adj.setdefault(a, []).append(b)
            nodes.add(a)
            nodes.add(b)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in nodes}

        def dfs(n: str) -> None:
            color[n] = GRAY
            for m in adj.get(n, []):
                if color.get(m) == GRAY:
                    raise TaskGraphError("任务依赖存在循环")
                if color.get(m) == WHITE:
                    dfs(m)
            color[n] = BLACK

        for n in nodes:
            if color[n] == WHITE:
                dfs(n)

    # ---------- 调度循环（可重入，重启后可反复调用） ----------

    def tick(self, root_id: str) -> dict:
        if safety.is_frozen(self.conn):
            return {"root_id": root_id, "status": "stopped", "advanced": False,
                    "reason": "global_freeze"}
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise FileNotFoundError(root_id)
        if root.status in TERMINAL_STATUSES:
            return {"root_id": root_id, "status": root.status, "advanced": False}

        with _root_lock(self.cfg.get("db_path", ""), root_id):
            return self._tick_locked(root_id)

    def _tick_locked(self, root_id: str) -> dict:
        root = db.get_task(self.conn, root_id)
        # 重启恢复：回收崩溃遗留的陈旧 in_progress（正在运行的不会被误回收）
        try:
            db.reset_stale_inprogress(self.conn)
        except Exception:  # noqa: BLE001
            pass
        steps = 0
        max_steps = self.limits.get("max_steps", 30)
        advanced_any = False

        self._resume_approved(root_id)

        while steps < max_steps:
            if safety.is_frozen(self.conn):
                break
            root = db.get_task(self.conn, root_id)
            if root.status in TERMINAL_STATUSES:
                break
            children = db.list_children(self.conn, root_id)
            self._cascade_failures(children)
            children = db.list_children(self.conn, root_id)

            ready = self._ready_actions(children)
            if not ready:
                break
            research = [t for t in ready if t.plan.get("subagent")]
            width = max(1, min(16, int(self.limits.get("max_parallel_subagents", 3))))
            batch = research[:min(width, max_steps - steps)]
            path = self.conn.execute("PRAGMA database_list").fetchone()["file"]
            if len(batch) > 1 and path:
                # Each worker owns its SQLite connection and runtime. The parent owns the
                # root scheduling lock; workers never recursively acquire that lock.
                with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="lf-branch") as pool:
                    advanced = list(pool.map(lambda t: self._run_parallel_node(path, t.id, root_id), batch))
                advanced_any = any(advanced) or advanced_any
                steps += len(batch)
            else:
                advanced_any = self._run_subtask(ready[0], root) or advanced_any
                steps += 1
            self._resume_approved(root_id)

        children = db.list_children(self.conn, root_id)
        cur_root = db.get_task(self.conn, root_id)
        if cur_root.status in TERMINAL_STATUSES:
            return {"root_id": root_id, "status": cur_root.status, "advanced": advanced_any}
        if children and all(c.status in TERMINAL_STATUSES for c in children):
            self._gate_exit(cur_root)
        elif not children and root.status not in (WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL, WAITING_APPROVAL):
            self._gate_exit(cur_root)

        return {"root_id": root_id, "status": db.get_task(self.conn, root_id).status,
                "advanced": advanced_any}

    def _run_parallel_node(self, path: str, task_id: str, root_id: str) -> bool:
        conn = db.connect(path)
        try:
            runtime = ToolRuntime(conn, self.runtime.registry, http=self.runtime.http,
                                  timeout_seconds=self.runtime.timeout_seconds)
            worker = Engine(conn, self.cfg, runtime)
            return worker._run_subtask(db.get_task(conn, task_id), db.get_task(conn, root_id))
        finally:
            conn.close()

    def _resume_approved(self, root_id: str) -> None:
        rows = self.conn.execute(
            """SELECT a.* FROM approvals a JOIN tasks t ON t.id=a.task_id
               WHERE t.root_id=? AND a.status IN ('approved','rejected')""",
            (root_id,),
        ).fetchall()
        for r in rows:
            task = db.get_task(self.conn, r["task_id"])
            if task is None or task.status != WAITING_APPROVAL:
                continue
            if r["status"] == "rejected":
                db.update_task(self.conn, task.id, status=FAILED,
                               result={"error": "审批被拒绝，动作未执行", "code": "approval_rejected",
                                       "approval_id": r["id"]})
                events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="governance",
                            detail={"status": FAILED, "reason": "approval_rejected"})
            else:
                db.update_task(self.conn, task.id, status=READY, clear_schedule=True)
                events.emit(self.conn, events.RECOVERY, task_id=task.id, actor="runtime",
                            detail={"reason": "approval_granted_resume", "approval_id": r["id"]})

    def _cascade_failures(self, children: list) -> None:
        by_id = {c.id: c for c in children}
        for c in children:
            if c.status in (PENDING, READY, IN_PROGRESS, WAITING_APPROVAL, WAITING_EVENT, WAITING_USER, WAITING_EXTERNAL):
                for dep in c.depends_on:
                    d = by_id.get(dep)
                    if d and d.status in (FAILED, CANCELLED):
                        db.update_task(
                            self.conn, c.id, status=FAILED,
                            result={"error": f"前置任务「{d.title}」{d.status}，依赖未满足",
                                    "reason": "dependency_failed"},
                        )
                        events.emit(self.conn, events.TASK_STATUS, task_id=c.id, actor="scheduler",
                                    detail={"status": FAILED, "reason": "dependency_failed",
                                            "failed_dep": dep})
                        break

    def _next_actionable(self, children: list):
        ready = self._ready_actions(children)
        return ready[0] if ready else None

    def _ready_actions(self, children: list) -> list:
        # 仅在所有依赖完成后调度 READY（PENDING 先转 READY）。IN_PROGRESS 不在此返回：
        # 它要么正被某 worker 执行（有锁/有领取），要么由 reset_stale_inprogress 回收为 READY。
        by_id = {d.id: d for d in children}
        ready = []
        for c in children:
            if c.status == PENDING:
                def _dep_ok(dep):
                    d = by_id.get(dep)
                    return d is not None and d.status == COMPLETED
                deps_done = all(_dep_ok(dep) for dep in c.depends_on)
                if not deps_done:
                    continue
                db.update_task(self.conn, c.id, status=READY)
                c = db.get_task(self.conn, c.id)
            if c.status == RETRYING:
                # 恢复循环再次领取：有界重试的节点重新入队
                db.update_task(self.conn, c.id, status=READY)
                c = db.get_task(self.conn, c.id)
            if c.status == READY:
                ready.append(c)
        return ready

    # ---------- 子任务执行 ----------


    # ======================================================================
    # 领域驱动主链路（Router -> 澄清 -> Coordinator 动态任务图 -> 固定 Subagent）
    # 与旧 scenario 模板路径并存；默认 /api/tasks 走本路径，旧 scenario 显式选择仍兼容。
    # ======================================================================

    def _run_subagent_node(self, task, root) -> bool:
        started_at, started = db.now(), time.monotonic()
        meta = {"root_id": root.id, "task_id": task.id, "branch": task.plan.get("branch"),
                "subagent": task.plan.get("subagent"), "started_at": started_at}
        events.emit(self.conn, "subagent_started", task_id=task.id, actor="scheduler", detail=meta)
        try:
            return self._execute_subagent_node(task, root)
        except Exception as exc:
            return self._handle_node_failure(task, task.plan.get("subagent"),
                                             {"ok": False, "error": f"execution_exception:{type(exc).__name__}"}, root)
        finally:
            current = db.get_task(self.conn, task.id)
            events.emit(self.conn, "subagent_finished", task_id=task.id, actor="scheduler",
                        detail={**meta, "ended_at": db.now(),
                                "duration_ms": round((time.monotonic() - started) * 1000),
                                "status": current.status, "error": current.result.get("error")})

    def _execute_subagent_node(self, task, root) -> bool:
        """执行一个 Coordinator 子节点（固定能力 subagent）。返回是否处理了该任务。"""
        sub_id = task.plan.get("subagent")
        if not sub_id:
            return False
        # 汇总依赖节点的证据作为纯计算节点输入
        # 统一节点输出协议：计算型子节点优先消费"标准化结果"，无则回退原始 evidence。
        # 标准化记录带 entity_canonical/规范单位/证据 id/转换记录，保证核验/对比对齐并可追溯。
        dep_records: list = []
        for dep_id in task.depends_on:
            dep = db.get_task(self.conn, dep_id)
            if dep is None:
                continue
            dr = (dep.result or {})
            norm = dr.get("normalized") or []
            if norm:
                dep_records.extend(norm)
            else:
                dep_records.extend(dr.get("evidence", []) or [])
        req = self._build_sa_request(task.plan.get("request") or {})
        scenario = f"user:{self._root_ws(task.root_id, 'default')}:{'engineering'}"
        runner = SubagentRunner(self.runtime, cfg=self.cfg)
        res = runner.run(sub_id, req, task_id=task.id, root_id=task.root_id,
                         scenario=scenario, input_records=dep_records)
        out = res.to_dict()
        if res.needs_user:
            db.update_task(self.conn, task.id, status=WAITING_USER,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out})
            return True
        if not res.ok:
            return self._handle_node_failure(task, sub_id, out, root)
        saved = db.update_task(self.conn, task.id, status=COMPLETED,
                       expected_statuses=(IN_PROGRESS,), require_active_root=True,
                       result={"subagent": sub_id, "branch": task.plan.get("branch"),
                               "evidence": res.evidence, "normalized": res.normalized,
                               "findings": res.findings, "limitations": res.limitations})
        if not saved:
            return False  # Cancellation or recovery won; discard the late result.
        events.emit(self.conn, events.TASK_STATUS, task_id=task.id,
                    actor=f"subagent:{sub_id}",
                    detail={"status": COMPLETED, "evidence": len(res.evidence),
                            "branch": task.plan.get("branch")})
        return True

    def _handle_node_failure(self, task, sub_id: str, out: dict, root) -> bool:
        """节点失败分流：可恢复瞬时错误 -> RETRYING（有界），其余 -> FAILED（分支可部分失败）。

        已完成工作与证据不丢：retry_count/last_error/recovery_steps 持久化在 plan/result；
        重试只重跑该节点，汇聚节点仍可使用其他成功分支证据。
        """
        max_retries = int((self.cfg or {}).get("limits", {}).get("max_node_retries", 2))
        plan = dict(task.plan or {})
        retry_count = int(plan.get("retry_count", 0))
        error = out.get("error")
        retriable = _is_retriable_error(error) and retry_count < max_retries
        recovery_steps = [
            f"重跑 subagent「{sub_id}」（不重放副作用，只读研究节点无外部写）",
            "若再次失败则继续计数，超过上限后该分支标记失败并保留其他分支结果",
        ]
        if retriable:
            retry_count += 1
            plan["retry_count"] = retry_count
            saved = db.update_task(self.conn, task.id, status=RETRYING, plan=plan,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out,
                                   "retry_count": retry_count, "max_retries": max_retries,
                                   "last_error": error,
                                   "recovery_steps": recovery_steps})
            if not saved:
                return False
            events.emit(self.conn, events.RECOVERY, task_id=task.id,
                        actor=f"subagent:{sub_id}",
                        detail={"status": RETRYING, "error": error,
                                "retry_count": retry_count, "max_retries": max_retries,
                                "branch": task.plan.get("branch")})
        else:
            # 超上限或非瞬时：FAILED（并行分支允许部分失败，不伪装成功）
            exhausted = retry_count >= max_retries and _is_retriable_error(error)
            saved = db.update_task(self.conn, task.id, status=FAILED,
                           expected_statuses=(IN_PROGRESS,), require_active_root=True,
                           result={"subagent": sub_id, **out,
                                   "retry_count": retry_count,
                                   "gave_up_retrying": bool(exhausted)})
            if not saved:
                return False
            events.emit(self.conn, events.ERROR, task_id=task.id,
                        actor=f"subagent:{sub_id}",
                        detail={"error": error, "branch": task.plan.get("branch"),
                                "retry_count": retry_count, "exhausted": exhausted})
        return True

    @staticmethod
    def _build_sa_request(raw: dict):
        return sa_mod.SubagentRequest(
            query=raw.get("query", ""),
            target_sites=raw.get("target_sites", []) or [],
            allowed_domains=raw.get("allowed_domains", []) or [],
            required_fields=raw.get("required_fields", []) or [],
            time_range=raw.get("time_range"),
            max_sources=int(raw.get("max_sources", 8) or 8),
            allow_followup=bool(raw.get("allow_followup", False)),
            completion_criteria=raw.get("completion_criteria", ""),
            authorization=raw.get("authorization", ""),
            extra=raw.get("extra", {}) or {},
        )


    def _build_action(self, task, root) -> dict:
        """决定子任务下一步动作。模板 tool_args 优先（场景配置），否则交给 LLM 驱动。"""
        tool = task.plan.get("tool_hint")
        tool_args = task.plan.get("tool_args")
        if tool and tool_args:
            # 模板动作：场景配置的确定性动作，不经模型，标记来源便于审计分流。
            return {"type": llm.ACTION_TOOL_CALL, "tool": tool,
                    "args": _coerce_numbers(_interp(tool_args, task.slots, root.objective)),
                    "reason": task.objective, "_source": "template"}
        ctx = {
            "goal": root.objective,
            "tool_hint": tool,
            "available_tools": [tool] if tool else self.runtime.registry.names(),
            "tool_specs": [
                {"name": spec.name, "description": spec.description, "parameters": spec.params_schema}
                for name, spec in self.runtime.registry.all().items()
                if not tool or name == tool
            ],
        }
        return self.driver.next_action(task, ctx)

    def _run_subtask(self, task, root) -> bool:
        if safety.is_frozen(self.conn):
            return False
        # 取消/终态守卫：根或自身已终结则不再执行（迟到 tick 不覆盖已取消状态）。
        if root.status in TERMINAL_STATUSES or task.status in TERMINAL_STATUSES:
            return False
        if task.status != IN_PROGRESS:
            # 原子领取：READY/PENDING -> IN_PROGRESS，避免并发 worker 重复领取。
            if not db.transition_status(self.conn, task.id, (PENDING, READY), IN_PROGRESS):
                return False  # 已被其他 worker 领取
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"from": task.status, "to": IN_PROGRESS, "status": IN_PROGRESS})

        # Explicit fixed-capability agent requests use the same scheduler and audit gates.
        if task.plan.get("subagent"):
            return self._run_subagent_node(task, root)
        via_template = bool(task.plan.get("tool_hint") and task.plan.get("tool_args"))
        action = self._build_action(task, root)
        if getattr(self.driver, "name", "local") in ("local", "base"):
            events.emit(self.conn, events.LLM_CALL, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"action": action.get("type"), "tool": action.get("tool"),
                                "driver": "rule", "real_model_call": False,
                                "via": "template" if via_template else "local_rules"})
        else:
            self._record_driver_reports(task)  # 抽取真实 HTTP 调用（usage/降级/重试）
            events.emit(self.conn, events.LLM_CALL, task_id=task.id,
                        actor=f"agent:{task.agent_role}",
                        detail={"action": action.get("type"), "tool": action.get("tool"),
                                "driver": self.driver.name,
                                "model": getattr(self.driver, "model", None),
                                "real_model_call": True})
        try:
            if db.is_status(self.conn, task.root_id, CANCELLED) or \
               db.is_status(self.conn, task.id, CANCELLED, FAILED, COMPLETED):
                return False  # 执行期间被取消/已终结：丢弃迟到结果
            if action["type"] == llm.ACTION_TOOL_CALL and action.get("tool"):
                expected = task.plan.get("tool_hint")
                if expected and action["tool"] != expected:
                    raise RuntimeError(f"子任务工具范围不匹配：预期 {expected}，收到 {action['tool']}")
                result = self.runtime.call(
                    action["tool"], action.get("args", {}), task,
                    reason=action.get("reason", ""),
                )
                self._after_tool(task, action["tool"], result)
                return True
            if action["type"] in (llm.ACTION_ANSWER, llm.ACTION_RETRY):
                self._finish_task(task, root)
                return True
        except permissions.ApprovalRequired as exc:
            if db.is_status(self.conn, task.root_id, CANCELLED):
                return
            db.update_task(self.conn, task.id, status=WAITING_APPROVAL,
                           result={"pending_approval": exc.approval_id, "tool": exc.tool_name})
            return False  # 不阻塞其他分支
        except ToolResultInDoubt as exc:
            # 副作用工具结果未知（本地不可中断超时）：进入待人工确认，绝不自动重试。
            db.update_task(self.conn, task.id, status=WAITING_EVENT,
                           result={"in_doubt": True, "tool": exc.tool_name,
                                   "error": exc.reason, "code": "result_in_doubt",
                                   "message": "动作可能已执行但结果未确认，需人工核对后再决定是否重试"})
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="runtime",
                        detail={"from": task.status, "to": WAITING_EVENT,
                                "status": WAITING_EVENT, "in_doubt": True, "tool": exc.tool_name})
            return False
        except permissions.PermissionDenied as exc:
            db.update_task(self.conn, task.id, status=FAILED,
                           result={"error": f"越权被阻止：{exc.reason}", "code": "permission_denied"})
            events.emit(self.conn, events.ERROR, task_id=task.id, actor="governance",
                        detail={"code": "permission_denied", "reason": exc.reason})
            return True
        except Exception as exc:  # noqa: BLE001 - 工具失败如实落账，不描述为成功
            db.update_task(self.conn, task.id, status=FAILED,
                           result={"error": f"执行失败：{str(exc)[:300]}", "code": "tool_failure"})
            events.emit(self.conn, events.ERROR, task_id=task.id, actor="runtime",
                        detail={"error": str(exc)[:300]})
            return True
        return False

    def _after_tool(self, task, tool: str, result: dict) -> None:
        # 终态守卫：工具返回时若根或本任务已取消/终结（取消恰发生在工具执行期间），
        # 不写 COMPLETED，避免迟到结果覆盖 CANCELLED。
        if db.is_status(self.conn, task.root_id, CANCELLED):
            return
        if db.is_status(self.conn, task.id, CANCELLED, FAILED):
            return
        if tool == "kb_search":
            chunks = result.get("chunks", [])
            # 检索员只产出证据；核验员（依赖检索员）负责起草与引用核验，避免答案重复
            if task.agent_role == ROLE_RESEARCHER:
                db.update_task(self.conn, task.id, status=COMPLETED,
                               result={"chunks": chunks, "evidence_count": result.get("count", 0)})
                events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                            detail={"status": COMPLETED, "evidence_count": len(chunks)})
            else:
                db.update_task(self.conn, task.id,
                               result={"chunks": chunks, "evidence_count": result.get("count", 0)})
                self._finish_task(task)
        else:
            # 工程执行工具：保留真实返回值与审计事件
            db.update_task(self.conn, task.id, status=COMPLETED,
                           result={"tool_result": result, "tool": tool})
            events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                        detail={"status": COMPLETED, "tool": tool})

    # ---------- 汇总/起草/引用核验 ----------

    def _gather_evidence(self, task) -> list[dict]:
        """Collect the local evidence produced by this task graph."""
        chunks = []
        for child in db.list_children(self.conn, task.root_id):
            chunks.extend(child.result.get("chunks") or [])
        return task.result.get("chunks") or chunks

    def _finish_task(self, task, root=None) -> None:
        root_task = root or db.get_task(self.conn, task.root_id)
        # 迟到的草稿/核验结果不得覆盖已取消或已终结的任务。
        if root_task.status in TERMINAL_STATUSES or task.status in TERMINAL_STATUSES:
            if root_task.status == CANCELLED or task.status == CANCELLED:
                return
        chunks = self._gather_evidence(task)

        if getattr(self.driver, "name", "local") not in ("local", "base"):
            draft = self.driver.draft_answer(root_task.objective, chunks, task.slots)
            self._record_driver_reports(task)
        else:
            draft = self.driver.draft_answer(root_task.objective, chunks, task.slots)
        conflicts = self._detect_conflicts(chunks)
        answer_text = draft.get("text", "")
        has_factual = bool(answer_text.strip()) and not draft.get("no_evidence", not chunks)
        vres = verification.verify_answer(
            answer_text, draft.get("citations", []), chunks,
            has_factual_answer=has_factual,
        )
        vdict = vres.to_dict()
        result = {
            "answer": answer_text,
            "citations": [
                {"chunk_id": c["chunk_id"], "doc_name": c["doc_name"], "section": c["section"],
                 "citations": c.get("citations", []), "snippet": c["text"][:200]}
                for c in chunks if c["chunk_id"] in draft.get("citations", [])
            ],
            # 四档核验结论：verified / partial / insufficient / failed
            "verification": vdict,
            "citation_check": {"passed": vdict["passed"], "problems": vdict["problems"]},
            "conflicts": conflicts,
            "no_evidence": vres.verdict == verification.INSUFFICIENT
                          or draft.get("no_evidence", not chunks),
        }
        db.update_task(self.conn, task.id, status=COMPLETED, result=result)
        events.emit(self.conn, events.TASK_STATUS, task_id=task.id, actor="scheduler",
                    detail={"status": COMPLETED,
                            "citations": len(result.get("citations", [])),
                            "conflicts": len(result.get("conflicts", []))})

    @staticmethod
    def _detect_conflicts(chunks: list[dict]) -> list[dict]:
        """检测同时有效的不同资料对同一指标给出冲突数值。

        已过期/已失效的版本（expires_at 早于今天）已在检索阶段排除、不参与回答，
        因此不再与现行版本构成"冲突"；只比较当前有效资料之间的矛盾。
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        # Reuse claim extraction so values only compete within the same metric/unit.
        by_doc: dict[str, list] = {}
        for ch in chunks:
            exp = ch.get("expires_at")
            if exp and exp < today:
                continue  # 已失效版本不参与冲突判定
            by_doc.setdefault(ch["doc_name"], []).append(ch)
        groups: dict[str, list[str]] = {}
        for name in by_doc:
            stem = re.sub(r"[_\-]?20\d{2}", "", name)
            groups.setdefault(stem, []).append(name)
        conflicts = []
        for stem, names in groups.items():
            if len(names) <= 1:
                continue
            metrics = {}
            for name in names:
                text = " ".join(c["text"] for c in by_doc[name])
                for claim in verification.extract_claims(text):
                    if claim.kind == "number" and claim.metric and claim.unit:
                        values = metrics.setdefault((claim.metric, claim.unit), {})
                        values.setdefault(name, set()).add(claim.value)
            for (metric, unit), vals in metrics.items():
                if len(vals) > 1 and len({tuple(sorted(v)) for v in vals.values()}) > 1:
                    conflicts.append({
                        "topic": stem, "metric": metric, "unit": unit,
                        "sources": list(vals),
                        "values": {k: sorted(v) for k, v in vals.items()},
                        "message": "不同版本资料对同一指标给出的数值不一致，需人工确认适用版本",
                    })
        return conflicts

    # ---------- 出口闸门 ----------

    def _gate_exit(self, root) -> None:
        """Aggregate actual execution states; completion is not candidate acceptance."""
        children = db.list_children(self.conn, root.id)
        if not children or any(c.status not in TERMINAL_STATUSES for c in children):
            return
        failed = [c.id for c in children if c.status != COMPLETED]
        status = FAILED if failed else COMPLETED
        db.update_task(self.conn, root.id, status=status,
                       result={"failed_tasks": failed, "verified": False,
                               "message": "Agent execution finished; candidate acceptance requires explicit verification."})
        events.emit(self.conn, events.TASK_STATUS, task_id=root.id, actor="scheduler",
                    detail={"status": status, "failed_tasks": failed})

    def user_message(self, root_id: str, text: str, slot_updates: dict | None = None) -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None:
            raise FileNotFoundError(root_id)
        events.emit(self.conn, events.MESSAGE, task_id=root_id, actor="user",
                    detail={"text": text, "slots": slot_updates or {}})
        if re.fullmatch(r"\s*(?:请)?(?:停止|暂停|冻结|不要继续修改|stop|pause|freeze)[！!。．.\s]*", text, re.I):
            safety.freeze(self.conn, actor=self._root_owner(root_id), reason=text)
            return {"status": "stopped", "reason": "global_freeze"}
        # Messages never mutate an approved contract or authorize execution.
        return {"status": root.status, "message": "需求补充已记录；请在开发契约中明确确认范围。"}

    def cancel(self, root_id: str, reason: str = "用户取消") -> dict:
        root = db.get_task(self.conn, root_id)
        if root is None or root.status in TERMINAL_STATUSES:
            return {"status": root.status if root else "not_found"}
        for c in db.list_children(self.conn, root_id):
            if c.status not in TERMINAL_STATUSES:
                db.update_task(self.conn, c.id, status=CANCELLED,
                               result={"cancelled_reason": reason})
        done = self.conn.execute(
            """SELECT detail_json FROM events WHERE kind='tool_result'
               AND json_extract(detail_json,'$.idempotency_key') IS NOT NULL
               AND json_extract(detail_json,'$.ok')=1
               AND task_id IN (SELECT id FROM tasks WHERE root_id=?)""",
            (root_id,),
        ).fetchall()
        irreversible = [json.loads(r["detail_json"]).get("result") for r in done]
        db.update_task(self.conn, root_id, status=CANCELLED,
                       result={"cancelled_reason": reason, "irreversible_side_effects": irreversible})
        events.emit(self.conn, events.TASK_STATUS, task_id=root_id, actor="user",
                    detail={"status": CANCELLED, "reason": reason,
                            "irreversible_count": len(irreversible)})
        return {"status": CANCELLED, "irreversible_side_effects": irreversible}

    def decide_approval(self, approval_id: str, decision: str, by: str = "user") -> dict:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(approval_id)
        if row["status"] != "pending":
            return {"status": row["status"]}
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 必须是 approved 或 rejected")
        self.conn.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
            (decision, by, db.now(), approval_id),
        )
        self.conn.commit()
        events.emit(self.conn, events.APPROVAL_DECIDED, task_id=row["task_id"], actor=by,
                    detail={"approval_id": approval_id, "decision": decision,
                            "tool": row["tool_name"]})
        root = db.get_task(self.conn, row["task_id"])
        self.tick(root.root_id)
        return {"status": decision}


# ---------------------------------------------------------------------------
# 模块级函数式入口（供评测/外部调用；内部委托 Engine）
# ---------------------------------------------------------------------------

def _registry_for_conn(conn, cfg):
    """每个连接缓存一个工具注册表（仅受控核心工具），避免多次构建与双注册表不一致。"""
    cache = getattr(conn, "_lf_registry", None)
    if cache is not None:
        return cache
    from .tools import ToolRegistry, register_core_tools
    registry = ToolRegistry()
    register_core_tools(registry)
    try:
        conn._lf_registry = registry  # sqlite Connection 允许挂属性
    except Exception:  # noqa: BLE001
        pass
    return registry


def _engine(conn, cfg=None, registry=None):
    from .tools import ToolRuntime, ToolResultInDoubt
    cfg = cfg or cfg_mod.load_config()
    registry = registry or _registry_for_conn(conn, cfg)
    runtime = ToolRuntime(conn, registry,
                          timeout_seconds=cfg.get("limits", {}).get("tool_timeout_seconds", 30))
    return Engine(conn, cfg, runtime)


def create_goal(conn, goal_or_payload, scenario=None, slots=None, cfg=None):
    """模块级建目标。支持 (conn, goal, scenario, slots) 与 (conn, {goal, scenario, slots})。"""
    if isinstance(goal_or_payload, dict):
        goal = goal_or_payload.get("goal")
        scenario = goal_or_payload.get("scenario")
        slots = goal_or_payload.get("slots")
    else:
        goal = goal_or_payload
    eng = _engine(conn, cfg)
    return eng.create_goal(goal, scenario, slots)


def create_root_task(conn, goal, scenario, slots=None, cfg=None):
    return create_goal(conn, goal, scenario, slots, cfg)


def run_root(conn, root_id, cfg=None):
    """可重入的推进入口（重启后以新连接调用同一 DB 即可恢复）。"""
    eng = _engine(conn, cfg)
    return eng.tick(root_id)


def decide_approval(conn, approval_id, decision, args=None, cfg=None):
    eng = _engine(conn, cfg)
    # args 仅用于绑定校验；批准绑定在请求时已落库，这里忽略额外参数
    return eng.decide_approval(approval_id, decision if isinstance(decision, str)
                               else decision.get("decision", "approved"))
