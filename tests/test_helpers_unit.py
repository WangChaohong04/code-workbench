"""辅助函数单元测试（不依赖后端实现，任何时候都应通过）。

- canonical_json：SPEC §1 幂等键/批准绑定的 canonical 比对基础；
- 工程耗时归一化：精确验证秒、小时与分钟的换算；
- 词重叠核验：SPEC §4 出口闸门引用核验原理。
"""
from __future__ import annotations


import conftest as cf


def test_canonical_json_stable():
    a = cf.canonical_json({"b": 1, "a": [1, 2, {"x": "中"}]})
    b = cf.canonical_json({"a": [1, 2, {"x": "中"}], "b": 1})
    assert a == b
    # 键排序 + 无多余空白（canonical）
    assert a.index('"a"') < a.index('"b"')
    assert ": " not in a and ", " not in a
    # 中文不转义
    assert "中" in a


def test_duration_normalization():
    from longflow.subagent_runner import normalize_value
    assert normalize_value(120, unit="秒") == (2.0, "分钟")
    assert normalize_value(0, unit="秒") == (0.0, "分钟")
    assert normalize_value(2, unit="小时") == (120.0, "分钟")


def test_overlap_ratio_chinese():
    claim = "生产代码修改须经负责人审批并通过隔离测试"
    source = "第三条 代码审批规则：生产代码修改须经负责人审批，并通过隔离测试后方可执行。"
    r = cf.text_overlap_ratio(claim, source)
    assert r >= 0.6, f"支撑性文本重叠应较高: {r}"
    r2 = cf.text_overlap_ratio("天空中的流星散落在遥远银河", source)
    assert r2 < 0.4, f"无关论断重叠应较低: {r2}"


def test_overlap_ratio_english():
    claim = "engineering build latency threshold"
    source = "The engineering build latency threshold is 500 milliseconds."
    assert cf.text_overlap_ratio(claim, source) >= 0.7
    assert cf.text_overlap_ratio("galaxy stellar radiation", source) < 0.3


def test_cjk_bigram_tokenizer():
    toks = cf._content_tokens("代码审批规则")
    # bigram：代码/码审/审批/批规/规则
    assert {"代码", "审批", "规则"} <= toks
