"""配置加载模块."""
import json
import os
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_CONFIG: Dict[str, Any] = {
    "database": {
        "path": "./audit_data/audit.db",
    },
    "csv": {
        "location_column": "location",
        "sku_column": "sku",
        "expected_column": "expected_qty",
        "counted_column": "counted_qty",
        "encoding": "utf-8-sig",
        "delimiter": ",",
    },
    "rules": {
        "diff_threshold": 0,
        "merge_keys": ["location", "sku"],
    },
    "status": {
        "initial": "pending",
        "allowed": ["pending", "confirmed", "ignored", "closed"],
    },
    "export": {
        "output_dir": "./audit_data/exports",
    },
}


def load_config(config_path: str = None) -> Dict[str, Any]:
    """加载配置文件，不存在则返回默认配置.

    Args:
        config_path: 配置文件路径，支持 JSON 格式

    Returns:
        配置字典
    """
    config = DEFAULT_CONFIG.copy()

    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8-sig") as f:
            user_config = json.load(f)
        _deep_merge(config, user_config)

    return config


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """深度合并字典，override 覆盖 base."""
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def get_db_path(config: Dict[str, Any]) -> str:
    """获取数据库文件绝对路径."""
    path = config["database"]["path"]
    return os.path.abspath(path)


def get_rules(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取规则配置，确保字段完整."""
    rules = config.get("rules", {})
    return {
        "diff_threshold": float(rules.get("diff_threshold", 0)),
        "merge_keys": list(rules.get("merge_keys", ["location", "sku"])),
    }


def get_allowed_statuses(config: Dict[str, Any]) -> List[str]:
    """获取允许的状态列表，来自配置而非硬编码."""
    return list(config.get("status", {}).get("allowed", []))


def ensure_dirs(config: Dict[str, Any]) -> None:
    """确保必要的目录存在."""
    db_path = get_db_path(config)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    export_dir = os.path.abspath(config["export"]["output_dir"])
    os.makedirs(export_dir, exist_ok=True)
