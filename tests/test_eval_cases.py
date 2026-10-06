"""Functional engineering safety cases for the current safety/tool APIs.

These tests intentionally exercise persisted state and tool events rather than
model wording or callable-only fixture placeholders.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from longflow import db, events, permissions, safety
from longflow.plugins.sdk import ToolSpec
from longflow.tools import ToolRegistry, ToolRuntime


READ = {"name": "kb_search", "risk": "low", "side_effect": False}
WRITE = {"name": "record_note", "risk": "medium", "side_effect": True}


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    safety.init_schema(conn)
    return conn


def _task(conn, owner="alice"):
    root = db.insert_task(conn, title="Engineering", kind="root", agent_role="controller",
                          objective="Review repository", root_id="", owner=owner)
    safety.ensure_contract(conn, root, "Review repository", owner=owner)
    return db.get_task(conn, root)


def _confirm(conn, task, **kwargs):
    return safety.confirm_contract(conn, task.root_id, actor="alice", confidence="high",
                                   allowed_tools=["record_note"], **kwargs)


def test_missing_contract_stays_readonly():
    conn = _conn()
    try:
        task = _task(conn)
        contract = safety.get_contract(conn, task.root_id)
        assert contract["mode"] == "read_only"
        assert contract["allowed_tools"] == []
        with pytest.raises(permissions.PermissionDenied, match="read-only"):
            safety.check_tool(conn, task, "record_note", {"text": "x"}, WRITE)
    finally:
        conn.close()


def test_no_verification_evidence_is_not_success():
    conn = _conn()
    try:
        task = _task(conn)
        safety.record_execution(conn, task.root_id, "succeeded")
        view = safety.snapshot(conn, task.root_id)
        assert view["progress"]["execution_status"] == "succeeded"
        assert view["progress"]["verification_status"] == "not_started"
        assert view["progress"]["verification_status"] != "passed"
    finally:
        conn.close()


def test_denied_scope_tool_is_rejected():
    conn = _conn()
    try:
        task = _task(conn)
        _confirm(conn, task, resources={"path": ["notes/approved.txt"]})
        assert safety.check_tool(conn, task, "record_note",
                                 {"path": "notes/approved.txt", "text": "ok"}, WRITE)
        with pytest.raises(permissions.PermissionDenied, match="Resource"):
            safety.check_tool(conn, task, "record_note",
                              {"path": "notes/secret.txt", "text": "no"}, WRITE)
        denied = conn.execute("SELECT detail_json FROM events WHERE kind='safety_tool_denied'").fetchall()
        assert denied
        assert "notes/secret.txt" not in json.dumps([dict(x) for x in denied])
    finally:
        conn.close()


def test_freeze_unfreeze_makes_persisted_contract_stale():
    directory = Path('.lfwork') / ('eval-restart-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    path = directory / "safety.sqlite"
    conn = db.connect(str(path))
    db.init_db(conn)
    safety.init_schema(conn)
    task = _task(conn)
    _confirm(conn, task)
    before = safety.get_epoch(conn)
    safety.freeze(conn, actor="alice", reason="stop")
    conn.close()

    restarted = db.connect(str(path))
    try:
        db.init_db(restarted)
        safety.init_schema(restarted)
        assert safety.is_frozen(restarted)
        safety.unfreeze(restarted, actor="alice")
        assert safety.get_epoch(restarted) == before + 2
        persisted = db.get_task(restarted, task.id)
        with pytest.raises(permissions.PermissionDenied, match="stale"):
            safety.check_tool(restarted, persisted, "record_note", {"text": "old authority"}, WRITE)
    finally:
        restarted.close()


def test_tool_failure_is_recorded_as_failure_not_success():
    conn = _conn()
    try:
        task = _task(conn)
        _confirm(conn, task)
        registry = ToolRegistry()

        def fail(_args, _ctx):
            return {"ok": False, "error": "disk unavailable"}

        registry.register(ToolSpec("record_note", "test writer", fail, risk="low",
                                   side_effect=True,
                                   params_schema={"properties": {"text": {"type": "string", "required": True}}}))
        approval_id = db.new_id("ap")
        args = {"text": "x"}
        conn.execute("INSERT INTO approvals(id,task_id,tool_name,args_json,args_hash,status,created_at) VALUES(?,?,?,?,?,?,?)",
                     (approval_id, task.id, "record_note", json.dumps(args), permissions.args_hash("record_note", args), "approved", db.now()))
        permissions.migrate_approval_schema(conn)
        epoch = safety.get_epoch(conn)
        conn.execute("UPDATE approvals SET epoch=?, expires_at=?, environment_fingerprint=? WHERE id=?",
                     (epoch, "2999-01-01T00:00:00+00:00", permissions._approval_env("record_note", args, epoch, conn=conn, task_id=task.id), approval_id))
        conn.commit()
        runtime = ToolRuntime(conn, registry)
        with pytest.raises(RuntimeError, match="执行失败"):
            runtime.call("record_note", {"text": "x"},
                         SimpleNamespace(id=task.id, root_id=task.root_id,
                                         agent_role="executor", slots={}, scenario="engineering"))
        result = [e for e in db_events(conn) if e["kind"] == events.TOOL_RESULT]
        assert result and result[-1]["detail"]["ok"] is False
        assert safety.snapshot(conn, task.root_id)["progress"]["execution_status"] == "failed"
    finally:
        conn.close()


def db_events(conn):
    rows = conn.execute("SELECT kind, detail_json FROM events ORDER BY id").fetchall()
    return [{"kind": r["kind"], "detail": json.loads(r["detail_json"] or "{}")} for r in rows]
