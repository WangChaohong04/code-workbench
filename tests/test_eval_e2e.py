"""Engineering runtime persistence and tool registration contracts."""
from __future__ import annotations
import conftest as cf
cf.skip_if_no_backend()

def test_tmp_db_initializes_stack(workdir):
    session = cf.tmp_db(workdir / "db")
    try:
        names = session.tool_names()
        assert "kb_search" in names
        assert "record_note" in names
        assert "make_purchase" not in names
        assert "send_notification" not in names
    finally:
        session.close()

def test_run_goal_returns_trace(workdir):
    session = cf.tmp_db(workdir / "db")
    try:
        out = cf.run_goal(session, "工程任务的安全审批规则是什么？", "engineering")
        assert out["root"] is not None
        assert out["root"]["root_id"] == out["root_id"]
        assert any(e["kind"] == "task_created" for e in out["events"])
    finally:
        session.close()

def test_restart_reopens_same_db(workdir):
    d = workdir / "restart"
    s = cf.tmp_db(d)
    rid = s.create_root("工程任务的安全审批规则是什么？", "engineering")
    cf._drive(s, rid)
    n = len(s.events(rid)); s.close()
    s2 = cf.restart(d / "longflow.db", rid)
    try:
        assert s2.task(rid) is not None
        assert len(s2.events(rid)) >= n
    finally:
        s2.close()
