"""Batch2：事实-证据核验四档 + 反例（空引用/错误指标/条件相反/夹带无据）。"""
from longflow import verification as V


def _chunk(cid, text):
    return {"chunk_id": cid, "doc_name": "工程规范", "section": "延迟", "text": text}


def test_correct_amount_verified():
    chunks = [_chunk("c1", "接口延迟上限为 500毫秒。")]
    r = V.verify_answer("接口延迟上限是 500毫秒。", ["c1"], chunks)
    assert r.verdict == V.VERIFIED
    assert r.contradicted == 0


def test_wrong_amount_failed():
    """引用内容相关但数值错误：共享词重叠不得放行。"""
    chunks = [_chunk("c1", "接口延迟上限为 500毫秒。")]
    r = V.verify_answer("接口延迟上限是 800毫秒。", ["c1"], chunks)
    assert r.verdict == V.FAILED
    assert r.contradicted >= 1
    assert any("矛盾" in p.get("problem", "") for p in r.problems)


def test_empty_citation_insufficient_not_verified():
    """空引用：有事实性答案但零引用 => 不得判为已核验。"""
    chunks = [_chunk("c1", "接口延迟上限 500毫秒。")]
    r = V.verify_answer("接口延迟上限是 500毫秒。", [], chunks)
    assert r.verdict in (V.INSUFFICIENT, V.FAILED)
    assert r.verdict != V.VERIFIED
    assert any("未提供任何引用" in p.get("problem", "") for p in r.problems)


def test_missing_citation_failed():
    """引用了不存在的 chunk_id => 核验失败。"""
    chunks = [_chunk("c1", "接口延迟上限 500毫秒。")]
    r = V.verify_answer("标准 500毫秒。", ["c9"], chunks)
    assert r.verdict == V.FAILED
    assert "c9" in r.missing_citations


def test_condition_reversed_failed():
    """条件相反：证据"不得"而答案"可以" => 核验失败。"""
    chunks = [_chunk("c1", "工程流程不得绕过隔离测试。")]
    r = V.verify_answer("工程流程可以绕过隔离测试。", ["c1"], chunks)
    assert r.verdict == V.FAILED


def test_unsupported_extra_claim_partial_or_failed():
    """有效引用中夹带无据数字：有据部分通过，无据数字不得判为已核验。"""
    chunks = [_chunk("c1", "接口延迟上限 500毫秒。")]
    # 答案含一个证据里没有的数字（磁盘占用 200MB）
    r = V.verify_answer("接口延迟上限 500毫秒，磁盘占用 200MB。", ["c1"], chunks)
    assert r.verdict in (V.PARTIAL, V.FAILED)
    assert r.verdict != V.VERIFIED
    # 500 有据
    assert any(cl["status"] == "supported" for cl in r.claims)


def test_no_number_overlap_does_not_auto_pass():
    """词重叠高但数字不一致：不得仅凭词语重叠判正确。"""
    chunks = [_chunk("c1", "组件内存上限为 300毫秒。")]
    r = V.verify_answer("组件内存上限为 900毫秒。", ["c1"], chunks)
    assert r.verdict == V.FAILED


def test_unrelated_evidence_insufficient():
    """证据与问题无关 => 证据不足/失败，不判 verified。"""
    chunks = [_chunk("c1", "仓库默认分支名称为 main。")]
    r = V.verify_answer("接口延迟上限是500毫秒。", ["c1"], chunks)
    assert r.verdict in (V.INSUFFICIENT, V.PARTIAL, V.FAILED)
    assert r.verdict != V.VERIFIED


def test_result_dict_shape():
    chunks = [_chunk("c1", "延迟 500毫秒。")]
    r = V.verify_answer("延迟 500毫秒。", ["c1"], chunks).to_dict()
    assert set(["verdict", "passed", "claims", "problems", "counts", "missing_citations"]) <= set(r)
    assert r["verdict"] in ("verified", "partial", "insufficient", "failed")
