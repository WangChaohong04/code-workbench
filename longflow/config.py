"""配置加载：config/longflow.yaml + config/scenarios/*.yaml + 环境变量。

密钥只从环境变量读取，绝不写入配置文件、日志或仓库。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "longflow.yaml"
SCENARIOS_DIR = REPO_ROOT / "config" / "scenarios"

_DEFAULTS: dict[str, Any] = {
    "llm": {"driver": "local", "base_url": "", "model": "local-default"},
    "plugins": {},
    "limits": {
        "max_steps": 30,
        "max_llm_calls": 80,
        "tick_interval_seconds": 2,
        "tool_timeout_seconds": 30,
        "max_verify_rounds": 2,
        "max_node_retries": 2,
        "max_parallel_subagents": 3,
        "max_upload_bytes": 20_000_000,
        "max_extracted_bytes": 40_000_000,
        "upload_timeout_seconds": 30,
        "parse_timeout_seconds": 15,
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "execution_sandbox": {"enabled": False, "image": "", "trusted_image_digest": False,
                          "user": "65534:65534", "timeout_seconds": 60,
                          "memory_bytes": 268435456, "cpus": 1, "pids_limit": 64,
                          "tmpfs_bytes": 67108864, "max_output_bytes": 262144,
                          "cleanup_timeout_seconds": 5},
    "human_channel": None,  # 人工交接渠道；未配置时如实告知用户

}


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：结果中的嵌套 dict 均为新建副本，绝不与 base（如 _DEFAULTS）共享，
    避免对返回 cfg 的 setdefault/env 写入跨多次 load_config 累积造成配置污染。"""
    out = {}
    for k, v in base.items():
        out[k] = _deep_merge(v, {}) if isinstance(v, dict) else v
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = _deep_merge(v, {}) if isinstance(v, dict) else v
    return out


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """加载主配置；环境变量覆盖（LLM_*、LONGFLOW_DB）。"""
    path = Path(path) if path else CONFIG_PATH
    cfg = _deep_merge(_DEFAULTS, {})
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})

    # 环境变量覆盖
    if os.getenv("LONGFLOW_DRIVER"):
        cfg["llm"]["driver"] = os.environ["LONGFLOW_DRIVER"]
    if os.getenv("LLM_BASE_URL"):
        cfg["llm"]["base_url"] = os.environ["LLM_BASE_URL"]
    if os.getenv("LLM_MODEL"):
        cfg["llm"]["model"] = os.environ["LLM_MODEL"]
    cfg["llm"]["api_key"] = os.getenv("LLM_API_KEY", "")  # 只保留在内存
    if os.getenv("LONGFLOW_SANDBOX_ENABLED") == "1":
        cfg["execution_sandbox"]["enabled"] = True
    if os.getenv("LONGFLOW_SANDBOX_IMAGE"):
        cfg["execution_sandbox"]["image"] = os.environ["LONGFLOW_SANDBOX_IMAGE"]
    if os.getenv("LONGFLOW_SANDBOX_TRUSTED") == "1":
        cfg["execution_sandbox"]["trusted_image_digest"] = True
    cfg["db_path"] = os.getenv("LONGFLOW_DB") or str(REPO_ROOT / "data" / "longflow.db")

    return cfg


def list_scenarios() -> list[dict]:
    scenarios = []
    if not SCENARIOS_DIR.exists():
        return scenarios
    for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        scenarios.append(
            {
                "name": data.get("name", p.stem),
                "description": data.get("description", ""),
                "file": p.name,
            }
        )
    return scenarios


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"场景配置不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def public_config(cfg: dict) -> dict:
    """返回可安全展示给前端/日志的配置（脱敏，绝不含密钥）。"""
    return {
        "llm": {
            "driver": cfg["llm"]["driver"],
            "model": cfg["llm"].get("model", ""),
            "api_key_configured": bool(cfg["llm"].get("api_key")),
        },
        "limits": cfg["limits"],
        "product": "software_engineering",
    }

