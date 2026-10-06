"""权限决策：用户授权 + 任务范围 + 插件声明 + 系统规则共同约束。

插件清单声明权限不代表自动获得授权。risk 等级：
  low    只读/无副作用 → 自动允许（记日志）
  medium 有副作用但影响有限 → 必须精确、有效期内的逐次审批
  high   删除/发布/数据库变更等 → 必须逐次审批，且批准绑定具体任务、契约与参数
显式 deny 永远拒绝。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db

ALLOW = "allow"
DENY = "deny"
NEEDS_APPROVAL = "needs_approval"


@dataclass
class Decision:
    verdict: str
    reason: str = ""
    grant_id: str | None = None
    approval_id: str | None = None


def migrate_approval_schema(conn: sqlite3.Connection) -> None:
    """Idempotent approval hardening migration; db.init_db may call this."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(approvals)").fetchall()}
    additions = {
        "workspace": "TEXT NOT NULL DEFAULT 'default'",
        "root_id": "TEXT",
        "epoch": "INTEGER",
        "environment_fingerprint": "TEXT",
        "expires_at": "TEXT",
        "consumed_at": "TEXT",
    }
    for name, decl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE approvals ADD COLUMN {name} {decl}")
    conn.commit()


def _approval_env(tool_name: str, args: dict, epoch=None, *, conn=None, task_id=None) -> str:
    payload = canonical_args(tool_name, args) + (f"|epoch:{epoch}" if epoch is not None else "")
    if conn is not None and task_id:
        row = conn.execute("SELECT root_id,workspace,owner FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            from . import safety
            contract = safety.get_contract(conn, row["root_id"])
            payload += json.dumps({"task": task_id, "workspace": row["workspace"],
                                   "owner": row["owner"], "contract": contract},
                                  sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def consume_approval(conn: sqlite3.Connection, approval_id: str, *, task_id: str,
                     tool_name: str, args: dict, epoch=None) -> bool:
    """Atomically consume one approved action immediately before dispatch."""
    migrate_approval_schema(conn)
    env = _approval_env(tool_name, args, epoch, conn=conn, task_id=task_id)
    cur = conn.execute(
        """UPDATE approvals SET status='consumed', consumed_at=?
           WHERE id=? AND task_id=? AND tool_name=? AND status='approved'
             AND args_hash=? AND expires_at>?
             AND environment_fingerprint=?""",
        (db.now(), approval_id, task_id, tool_name, args_hash(tool_name, args), db.now(), env),
    )
    conn.commit()
    return cur.rowcount == 1


def canonical_args(tool_name: str, args: dict) -> str:
    payload = json.dumps(
        {"tool": tool_name, "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return payload


def args_hash(tool_name: str, args: dict) -> str:
    return hashlib.sha256(canonical_args(tool_name, args).encode("utf-8")).hexdigest()


def _object_id(tool_name: str, args: dict) -> str:
    """提取动作对象标识，用于 object_pattern 匹配（glob 可匹配）。"""
    for key in ("repository", "branch", "path", "file", "database", "environment", "url"):
        if key in args and args[key] is not None:
            return f"{key}:{args[key]}:*"
    return f"{tool_name}:*"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _grant_matches(grant: sqlite3.Row, tool_name: str, obj_id: str) -> bool:
    if grant["tool_name"] and grant["tool_name"] != tool_name:
        return False
    pattern = grant["object_pattern"]
    if pattern:
        # 工具级授权（pattern 形如 '<tool>:*'）同时匹配该工具的任意对象；
        # 具体对象 pattern（如 'channel:ops:*'）按 glob 匹配。
        tool_glob = f"{tool_name}:*"
        if not (fnmatch.fnmatch(obj_id, pattern) or pattern == tool_glob
                or fnmatch.fnmatch(tool_glob, pattern)):
            return False
    if grant["max_count"] is not None and (grant["used_count"] or 0) >= grant["max_count"]:
        return False
    exp = _parse_ts(grant["expires_at"])
    if exp is not None and exp < _now():
        return False
    return True


def decide(
    conn: sqlite3.Connection,
    tool_name: str,
    risk: str,
    args: dict,
    *,
    task_id: str | None = None,
    root_id: str | None = None,
) -> Decision:
    migrate_approval_schema(conn)
    obj_id = _object_id(tool_name, args)
    workspace = None
    if task_id:
        tr = conn.execute("SELECT workspace FROM tasks WHERE id=?", (task_id,)).fetchone()
        workspace = tr["workspace"] if tr else None

    grants = conn.execute(
        "SELECT * FROM grants WHERE (? IS NULL OR workspace=? OR workspace='*') "
        "ORDER BY CASE scope WHEN 'deny' THEN 0 WHEN 'once' THEN 1 ELSE 2 END, created_at",
        (workspace, workspace),
    ).fetchall()

    # 1) 显式 deny 优先
    for g in grants:
        if g["task_id"]:
            grant_task = conn.execute("SELECT root_id FROM tasks WHERE id=?", (g["task_id"],)).fetchone()
            if not grant_task or grant_task["root_id"] != root_id:
                continue
        if g["scope"] == "deny" and _grant_matches(g, tool_name, obj_id):
            return Decision(DENY, f"命中禁止授权规则（{g['object_pattern'] or tool_name}）", g["id"])

    if risk == "low":
        return Decision(ALLOW, "低风险只读操作自动允许")

    # All writes require exact-task, current-epoch, unexpired parameter-bound approval.
    from . import safety
    epoch = safety.get_epoch(conn)
    approved = conn.execute(
        """SELECT * FROM approvals WHERE task_id=? AND tool_name=? AND status='approved'
           AND args_hash=? AND expires_at>? AND epoch=? AND environment_fingerprint=?""",
        (task_id, tool_name, args_hash(tool_name, args), db.now(), epoch,
         _approval_env(tool_name, args, epoch, conn=conn, task_id=task_id)),
    ).fetchone()
    if approved:
        return Decision(ALLOW, "Exact single-use approval", approval_id=approved["id"])
    return Decision(NEEDS_APPROVAL, "Writes require current exact-task approval")


def _remaining(g: sqlite3.Row) -> str:
    if g["max_count"] is None:
        return "不限"
    return str(max(0, g["max_count"] - (g["used_count"] or 0)))


def increment_grant(conn: sqlite3.Connection, grant_id: str) -> None:
    conn.execute(
        "UPDATE grants SET used_count = used_count + 1 WHERE id=?", (grant_id,)
    )
    conn.commit()


def add_grant(
    conn: sqlite3.Connection,
    *,
    scope: str,
    tool_name: str | None = None,
    object_pattern: str | None = None,
    max_count: int | None = None,
    expires_at: str | None = None,
    granted_by: str = "system",
    task_id: str | None = None,
) -> str:
    gid = db.new_id("gr")
    conn.execute(
        """INSERT INTO grants (id, scope, tool_name, action, object_pattern, max_count,
           used_count, expires_at, granted_by, task_id, created_at)
           VALUES (?,?,?,?,?,?,0,?,?,?,?)""",
        (
            gid,
            scope,
            tool_name,
            None,
            object_pattern,
            max_count,
            expires_at,
            granted_by,
            task_id,
            db.now(),
        ),
    )
    conn.commit()
    return gid


def preauth_from_policy(
    conn: sqlite3.Connection, policy: dict, *, task_id: str | None = None
) -> list[str]:
    """把场景 policies.preauth 配置落地为 grants（幂等：同 task+tool+pattern 不重复）。"""
    ids = []
    for item in policy.get("preauth", []) or []:
        exists = conn.execute(
            "SELECT id FROM grants WHERE scope='preauth' AND task_id IS ? AND tool_name IS ? AND object_pattern IS ?",
            (task_id, item.get("tool"), item.get("object_pattern")),
        ).fetchone()
        if exists:
            ids.append(exists["id"])
            continue
        expires = None
        if item.get("expires_hours"):
            from datetime import timedelta

            expires = (datetime.now(timezone.utc) + timedelta(hours=item["expires_hours"])).isoformat(
                timespec="seconds"
            )
        ids.append(
            add_grant(
                conn,
                scope="preauth",
                tool_name=item.get("tool"),
                object_pattern=item.get("object_pattern"),
                max_count=item.get("max_count"),
                expires_at=expires,
                task_id=task_id,
            )
        )
    return ids


class PermissionDenied(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ApprovalRequired(Exception):
    def __init__(self, approval_id: str, tool_name: str, args: dict, reason: str):
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.args = args
        self.reason = reason
        super().__init__(f"需要审批: {tool_name} ({approval_id})")
