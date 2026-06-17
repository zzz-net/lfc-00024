"""配置加载模块."""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


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
    "active_plan": None,
    "operator": "cli",
}


def _state_file_path(config: Dict[str, Any]) -> str:
    """运行时状态文件路径（与数据库同目录，保存 active_plan / operator）."""
    db_dir = os.path.dirname(os.path.abspath(config["database"]["path"]))
    return os.path.join(db_dir, "runtime_state.json")


def load_config(config_path: str = None) -> Dict[str, Any]:
    """加载配置文件，不存在则返回默认配置，并合并运行时状态（active_plan/operator）.

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

    try:
        state_path = _state_file_path(config)
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if "active_plan" in state:
                config["active_plan"] = state["active_plan"]
            if "operator" in state:
                config["operator"] = state["operator"]
    except (OSError, json.JSONDecodeError):
        pass

    return config


def save_runtime_state(config: Dict[str, Any]) -> None:
    """将 active_plan / operator 落盘，确保重启后续用."""
    try:
        state_path = _state_file_path(config)
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        state = {
            "active_plan": config.get("active_plan"),
            "operator": config.get("operator", "cli"),
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def set_active_plan(config: Dict[str, Any], plan_name: Optional[str]) -> None:
    """设置当前方案并立即落盘."""
    config["active_plan"] = plan_name
    save_runtime_state(config)


def set_operator(config: Dict[str, Any], operator: str) -> None:
    """设置操作人并立即落盘."""
    config["operator"] = operator
    save_runtime_state(config)


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
