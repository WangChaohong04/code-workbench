"""批次4：RAG 文件导入——解析/预览/确认激活/旧版归档/隔离。"""
from __future__ import annotations

import pytest

from longflow import db, rag
from longflow import importer as IMP


def _conn(workdir):
    conn = db.connect(str(workdir / "imp.db"))
    db.init_db(conn)
    rag._ensure_rag_cols(conn)
    return conn


def test_parse_md_sections_and_version():
    doc = IMP.parse_text("policy.md", "# 工程规范\n版本 2025\n## 延迟\n上限 500 毫秒\n\n## 构建\n执行隔离测试")
    assert doc.title == "工程规范"
    assert doc.version == "2025"
    secs = [s["section"] for s in doc.sections]
    assert "延迟" in secs and "构建" in secs


def test_unsupported_extension_rejected():
    with pytest.raises(ValueError):
        IMP.parse_text("virus.exe", b"binary")


def test_preview_not_searchable_until_activated(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("kb.md", "# 组件规范\n## 内存\n编译服务内存上限 12345 MB")
    doc_id = IMP.preview(conn, workspace="ws1", owner="u1", domain="engineering", doc=doc)
    # 预览状态：检索不到
    hits = rag.search(conn, "编译服务 12345", scenario="user:ws1:engineering")
    assert all("编译服务" not in (c.get("text") or "") for c in hits)
    # 用户确认后激活
    n = IMP.activate(conn, doc_id, by="u1")
    assert n >= 1
    hits2 = rag.search(conn, "编译服务内存上限", scenario="user:ws1:engineering")
    assert any("12345" in (c.get("text") or "") for c in hits2)


def test_workspace_domain_isolation(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("a.md", "# 内部资料\n## 机密\n项目代号 蓝鹰 构建耗时 999 秒")
    doc_id = IMP.preview(conn, workspace="ws-secret", owner="u1", domain="engineering", doc=doc)
    IMP.activate(conn, doc_id, by="u1")
    # 不同 workspace 的 scenario 检索不到
    other = rag.search(conn, "蓝鹰 项目代号", scenario="user:ws-other:engineering")
    assert all("蓝鹰" not in (c.get("text") or "") for c in other)
    # 同 workspace 可检索
    same = rag.search(conn, "蓝鹰 项目代号", scenario="user:ws-secret:engineering")
    assert any("蓝鹰" in (c.get("text") or "") for c in same)


def test_old_version_archived_on_reimport(workdir):
    conn = _conn(workdir)
    d1 = IMP.parse_text("policy.md", "# 制度\n版本 v1\n## x\n旧规则 内容AAA")
    id1 = IMP.preview(conn, workspace="ws", owner="u", domain="engineering", doc=d1)
    IMP.activate(conn, id1, by="u")
    d2 = IMP.parse_text("policy.md", "# 制度\n版本 v2\n## x\n新规则 内容BBB")
    id2 = IMP.preview(conn, workspace="ws", owner="u", domain="engineering", doc=d2)
    IMP.activate(conn, id2, by="u")
    docs = {d["id"]: d["status"] for d in IMP.list_docs(conn, workspace="ws")}
    assert docs[id1] == IMP.STATUS_ARCHIVED
    assert docs[id2] == IMP.STATUS_ACTIVE
    # 检索命中新版内容
    hits = rag.search(conn, "规则 内容", scenario="user:ws:engineering")
    assert any("内容BBB" in (c.get("text") or "") for c in hits)


def test_preview_summary_shape(workdir):
    conn = _conn(workdir)
    doc = IMP.parse_text("p.md", "# T\n## S1\n正文内容若干。")
    doc_id = IMP.preview(conn, workspace="ws", owner="u", domain="d", doc=doc)
    s = IMP.preview_summary(conn, doc_id)
    assert s["status"] == IMP.STATUS_PREVIEW and s["section_count"] >= 1
    assert s["preview"][0]["text"]
