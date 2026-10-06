"""Focused regression tests for persisted, model-independent safety controls."""
import json
import sqlite3

import pytest

from longflow import db, events, permissions, safety


READ_SPEC = {"name": "kb_search", "risk": "low", "side_effect": False}
WRITE_SPEC = {"name": "record_note", "risk": "medium", "side_effect": True}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def make_root(conn):
    root_id = db.insert_task(
        conn, title="Engineering task", kind="task", agent_role="executor",
        objective="Inspect and document code", root_id="", owner="alice",
    )
    safety.ensure_contract(conn, root_id, "Inspect and document code", owner="alice")
    return db.get_task(conn, root_id)


def confirm_write(conn, root_id):
    return safety.confirm_contract(
        conn, root_id, actor="alice", confidence="high", allowed_tools=["record_note"]
    )


def check_write(conn, task):
    return safety.check_tool(conn, task, "record_note", {"text": "note"}, WRITE_SPEC)


def add_approval(conn, task_id, status):
    approval_id = db.new_id("ap")
    args = {"text": "note"}
    with conn:
        conn.execute(
            """INSERT INTO approvals
               (id,task_id,tool_name,args_json,args_hash,reason,status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (approval_id, task_id, "record_note", json.dumps(args),
             permissions.args_hash("record_note", args), "review", status, db.now()),
        )
    permissions.migrate_approval_schema(conn)
    epoch = safety.get_epoch(conn)
    conn.execute("UPDATE approvals SET epoch=?, expires_at=?, environment_fingerprint=? WHERE id=?",
                 (epoch, "2999-01-01T00:00:00+00:00",
                  permissions._approval_env("record_note", args, epoch, conn=conn, task_id=task_id), approval_id))
    conn.commit()
    return approval_id


def approval(conn, approval_id):
    return dict(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())


def test_new_contract_is_read_only_and_repeated_ensure_preserves_confirmation(conn):
    task = make_root(conn)
    contract = safety.get_contract(conn, task.id)
    assert (contract["confidence"], contract["mode"], contract["allowed_tools"]) == (
        "low", "read_only", []
    )
    assert safety.check_tool(conn, task, "kb_search", {}, READ_SPEC)["side_effect"] is False
    with pytest.raises(permissions.PermissionDenied, match="read-only"):
        check_write(conn, task)

    confirmed = confirm_write(conn, task.id)
    assert check_write(conn, task)["side_effect"] is True
    assert safety.ensure_contract(conn, task.id, "Changed goal", owner="alice") == confirmed


@pytest.mark.parametrize("confidence", ["low", "medium"])
def test_lower_confidence_discards_requested_and_previous_write_grants(conn, confidence):
    task = make_root(conn)
    previous = confirm_write(conn, task.id)
    check_write(conn, task)
    contract = safety.confirm_contract(
        conn, task.id, actor="alice", confidence=confidence, allowed_tools=["record_note"]
    )
    assert contract == safety.get_contract(conn, task.id)
    assert contract["confidence"] == confidence
    assert contract["mode"] == "read_only"
    assert contract["allowed_tools"] == []
    assert contract["revision"] == previous["revision"] + 1
    with pytest.raises(permissions.PermissionDenied, match="read-only"):
        check_write(conn, task)
    assert safety.check_tool(conn, task, "kb_search", {}, READ_SPEC)["risk"] == "low"


def test_high_confidence_without_explicit_tools_remains_read_only(conn):
    task = make_root(conn)
    contract = safety.confirm_contract(conn, task.id, actor="alice", confidence="high")
    assert contract["mode"] == "read_only"
    assert contract["allowed_tools"] == []
    with pytest.raises(permissions.PermissionDenied, match="read-only"):
        check_write(conn, task)


def test_freeze_blocks_all_roots_but_not_reads_and_unfreeze_requires_reconfirmation(conn):
    tasks = [make_root(conn), make_root(conn)]
    for task in tasks:
        confirm_write(conn, task.id)
        check_write(conn, task)
    epoch = safety.get_epoch(conn)
    frozen = safety.freeze(conn, actor="alice", reason="Stop new writes")
    assert frozen["frozen"] is True
    assert frozen["epoch"] == epoch + 1
    assert frozen["running_operations"] == "not_safely_interruptible"
    assert safety.is_frozen(conn) is True
    for task in tasks:
        with pytest.raises(permissions.PermissionDenied, match="freeze"):
            check_write(conn, task)
        assert safety.check_tool(conn, task, "kb_search", {}, READ_SPEC)["risk"] == "low"
        before = safety.get_contract(conn, task.id)
        with pytest.raises(permissions.PermissionDenied, match="frozen"):
            confirm_write(conn, task.id)
        assert safety.get_contract(conn, task.id) == before

    resumed = safety.unfreeze(conn, actor="alice")
    assert resumed["frozen"] is False
    assert resumed["epoch"] == epoch + 2
    for task in tasks:
        with pytest.raises(permissions.PermissionDenied, match="stale"):
            check_write(conn, task)
    confirm_write(conn, tasks[0].id)
    assert check_write(conn, tasks[0])["epoch"] == resumed["epoch"]
    with pytest.raises(permissions.PermissionDenied, match="stale"):
        check_write(conn, tasks[1])


@pytest.mark.parametrize("confidence", ["high", "medium"])
def test_contract_revision_revokes_only_its_root_approvals(conn, confidence):
    task, other = make_root(conn), make_root(conn)
    confirm_write(conn, task.id)
    child_id = db.insert_task(
        conn, title="Child", kind="task", agent_role="executor", objective="Document",
        root_id=task.id, parent_id=task.id, owner="alice",
    )
    pending = add_approval(conn, task.id, "pending")
    approved = add_approval(conn, child_id, "approved")
    rejected = add_approval(conn, child_id, "rejected")
    unrelated = add_approval(conn, other.id, "approved")
    args = {"text": "note"}
    assert permissions.decide(conn, "record_note", "medium", args,
                              task_id=child_id, root_id=task.id).verdict == permissions.ALLOW

    safety.confirm_contract(conn, task.id, actor="alice", confidence=confidence,
                            allowed_tools=["record_note"])
    for approval_id in (pending, approved):
        row = approval(conn, approval_id)
        assert row["status"] == "revoked"
        assert row["decided_by"] == "alice"
        assert row["decided_at"]
    assert approval(conn, rejected)["status"] == "rejected"
    assert approval(conn, unrelated)["status"] == "approved"
    assert permissions.decide(conn, "record_note", "medium", args,
                              task_id=child_id, root_id=task.id).verdict == permissions.NEEDS_APPROVAL


def test_freeze_and_unfreeze_revoke_approvals_globally_without_reviving_them(conn):
    first, second = make_root(conn), make_root(conn)
    pending = add_approval(conn, first.id, "pending")
    approved = add_approval(conn, second.id, "approved")
    rejected = add_approval(conn, second.id, "rejected")
    safety.freeze(conn, actor="alice")
    assert approval(conn, pending)["status"] == "revoked"
    assert approval(conn, approved)["status"] == "revoked"
    # Even a pending approval queued during the freeze cannot survive resume.
    queued = add_approval(conn, first.id, "pending")
    safety.unfreeze(conn, actor="alice")
    for approval_id in (pending, approved, queued):
        assert approval(conn, approval_id)["status"] == "revoked"
    assert approval(conn, rejected)["status"] == "rejected"


@pytest.mark.parametrize("mutation", [
    "UPDATE events SET detail_json='{}' WHERE id=?",
    "DELETE FROM events WHERE id=?",
])
@pytest.mark.parametrize("kind", ["safety_frozen", events.TOOL_RESULT])
def test_events_reject_mutation_and_still_allow_appends(conn, mutation, kind):
    event_id = events.emit(conn, kind, actor="alice", detail={"evidence": "original"})
    original = dict(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
    # Reinitialization must retain the triggers and existing audit evidence.
    db.init_db(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with conn:
            conn.execute(mutation, (event_id,))
    assert dict(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()) == original
    next_id = events.emit(conn, events.MESSAGE, detail={"evidence": "new"})
    assert next_id > event_id
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
