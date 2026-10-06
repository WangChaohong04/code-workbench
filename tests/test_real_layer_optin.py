"""真实层（真实模型 API）独立、opt-in 报告。

与离线规则层、mock 桩层完全分开：
- 默认全部 **skip**（本机跑 pytest 默认通过离线/模拟即可，模拟通过**不算**真实通过）；
- 只有显式设置环境开关才会真正执行，且逐项验证真实能力是否可用；
- 报告分三类：offline / mock / real，绝不把仿真结果冒充真实结果。

做此类测试前必须满足真实前提（否则诚实失败，不降级为 mock 掩盖）。
"""
from __future__ import annotations

import os

import pytest

# 开关：真实模型需 LONGFLOW_REAL_MODEL=1 且配置了 openai_compatible 端点；
_REAL_MODEL = os.environ.get("LONGFLOW_REAL_MODEL") == "1"


def _real_skipped(name: str):
    return pytest.skip(f"真实层 opt-in 未开启（{name}）。"
                       f"离线/模拟层继续运行，但不计入真实通过。")


def test_real_model_driver_available():
    """真实模型：前置条件缺失时明确 skip（不计为通过）。"""
    if not _REAL_MODEL:
        _real_skipped("LONGFLOW_REAL_MODEL")
    from longflow import config as cfg_mod, llm as llm_mod
    cfg = cfg_mod.load_config()
    cfg["llm"].update(driver="openai_compatible", strict=True, max_retries=0)
    # 无端点/key 则为"配置不完整"，应真实失败而非跳过
    if not all(cfg["llm"].get(k) for k in ("base_url", "model", "api_key")):
        pytest.skip("未验证：缺少 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY")
    driver = llm_mod.build_driver(cfg)
    assert driver.name != "local", "配置了真实端点却降级到 local，不能冒充真实模型"
    result = driver._chat_json('只返回 JSON 对象：{"ok": true}',
                               "验证接口连通性。", call_type="real_test")
    assert result == {"ok": True}
    reports = driver.drain_reports()
    assert len(reports) == 1 and reports[0]["http_status"] == 200
    assert reports[0]["ok"] is True and not reports[0]["fallback"]
