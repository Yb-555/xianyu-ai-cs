"""配置加载：读取 config.yml（不存在则回退到 config.example.yml）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
_CACHE: dict[str, Any] | None = None


def _config_path() -> Path:
    real = ROOT / "config.yml"
    if real.exists():
        return real
    return ROOT / "config.example.yml"


def load_config(reload: bool = False) -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE
    path = _config_path()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # 允许环境变量覆盖密钥，避免明文入库
    if os.getenv("AI_API_KEY"):
        cfg.setdefault("ai", {})["api_key"] = os.environ["AI_API_KEY"]
    if os.getenv("EMBEDDING_API_KEY"):
        cfg.setdefault("knowledge", {})["embedding_api_key"] = os.environ["EMBEDDING_API_KEY"]
    _CACHE = cfg
    return cfg


def get(path: str, default: Any = None) -> Any:
    """点号路径取值，如 get('ai.model')。"""
    cur: Any = load_config()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
