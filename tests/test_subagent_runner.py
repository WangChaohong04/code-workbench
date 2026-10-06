"""批次8：固定能力 Subagent 可实际执行——跑工具、归一证据、越权拦截、困难暂停。"""
from __future__ import annotations

import pytest

from longflow import subagents as sa
from longflow.subagent_runner import SubagentRunner
from longflow import evidence as E
import longflow.permissions as permissions


@pytest.fixture()
def runner(workdir):
    from tests.conftest import tmp_db
    session = tmp_db(workdir)
    from longflow import rag, db
    source = workdir / "engineering.md"
    source.write_text("# Engineering latency\n接口延迟上限为 500 毫秒，测试必须记录结果。", encoding="utf-8")
    rag.load_knowledge_path(session.conn, str(source), "engineering")
    root = session.engine().create_goal("Inspect engineering latency", "engineering")["task_id"]
    session.test_root = root
    return SubagentRunner(session.runtime), session


def test_rag_researcher_returns_evidence_records(runner):
    r, session = runner
    req = sa.SubagentRequest(query="接口延迟上限", max_sources=5)
    res = r.run(sa.SA_RAG_RESEARCHER, req, task_id=session.test_root, root_id=session.test_root, scenario="engineering")
    assert res.ok
    assert res.evidence, "应返回证据记录"
    rec = res.evidence[0]
    # 统一证据结构字段完备
    for key in ("entity", "source_type", "evidence_text", "layer", "quality"):
        assert key in rec
    # Subagent 结果不含最终决策字段
    assert not hasattr(res, "decision")
    assert any("延迟" in (e.get("evidence_text") or "") or "500" in (e.get("evidence_text") or "")
               for e in res.evidence)


def test_user_review_marks_opinions():
    # User review is opinion, not a verified fact
    rec = E.make_record(entity="代码评审", field="评价", value="好用",
                        source_type=E.SRC_DOCUMENT, layer=E.LAYER_OPINION)
    assert rec.layer == E.LAYER_OPINION and rec.source_type == "document"


def test_normalizer_standardizes_units():
    r = SubagentRunner(runtime=None)  # 纯计算型不需要 runtime
    records = [
        E.make_record(entity="A", field="build_time", value=120, unit="秒").to_dict(),
        E.make_record(entity="B", field="test_time", value=2, unit="小时").to_dict(),
    ]
    res = r.run(sa.SA_NORMALIZER, sa.SubagentRequest(), task_id="t", root_id="t",
                input_records=records)
    assert res.ok
    by_entity = {n["entity"]: n for n in res.normalized}
    assert by_entity["A"]["value"] == pytest.approx(2) and by_entity["A"]["unit"] == "分钟"
    assert by_entity["B"]["value"] == 120 and by_entity["B"]["unit"] == "分钟"


def test_evidence_verifier_detects_conflict():
    r = SubagentRunner(runtime=None)
    records = [
        E.make_record(entity="响应延迟", field="限额", value=400, unit="毫秒",
                      source_title="latency_2023.md").to_dict(),
        E.make_record(entity="响应延迟", field="限额", value=500, unit="毫秒",
                      source_title="latency_2024.md").to_dict(),
        E.make_record(entity="组件", field="评分", value="好", layer=E.LAYER_UNCONFIRMED).to_dict(),
    ]
    res = r.run(sa.SA_EVIDENCE_VERIFIER, sa.SubagentRequest(), task_id="t", root_id="t",
                input_records=records)
    assert res.ok
    assert len(res.findings["conflicts"]) == 1
    assert res.findings["layer_counts"].get(E.LAYER_UNCONFIRMED) == 1


def test_comparison_needs_criteria_then_builds_table():
    r = SubagentRunner(runtime=None)
    # 无比较标准 -> 请求用户确认标准
    res0 = r.run(sa.SA_COMPARISON, sa.SubagentRequest(query="对比"),
                 task_id="t", root_id="t", input_records=[])
    assert res0.ok is False and res0.needs_user is True
    # 给标准 + 事实证据 -> 出对照表（不含推荐）
    records = [
        E.make_record(entity="甲", field="latency", value=100, unit="毫秒").to_dict(),
        E.make_record(entity="乙", field="latency", value=80, unit="毫秒").to_dict(),
        E.make_record(entity="甲", field="推荐", value="选甲", layer=E.LAYER_RECOMMENDATION).to_dict(),
    ]
    res = r.run(sa.SA_COMPARISON, sa.SubagentRequest(query="对比", required_fields=["latency"]),
                task_id="t", root_id="t", input_records=records)
    assert res.ok
    ents = {row["entity"] for row in res.findings["comparison"]}
    assert {"甲", "乙"} <= ents
    # 推荐层不进入事实对照表
    assert all("推荐" not in row["attributes"] for row in res.findings["comparison"])


def test_subagent_whitelist_blocks_tool():
    # Read-only researcher cannot record notes.
    with pytest.raises(PermissionError):
        sa.assert_tool_allowed(sa.SA_RAG_RESEARCHER, "record_note")
    sa.assert_tool_allowed(sa.SA_RAG_RESEARCHER, "kb_search")


def test_subagent_missing_query_requests_user():
    r = SubagentRunner(runtime=None)
    res = r.run(sa.SA_RAG_RESEARCHER, sa.SubagentRequest(query=""), task_id="t", root_id="t")
    assert res.ok is False and res.needs_user is True
    assert "query" in (res.error or "")
