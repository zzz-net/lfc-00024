"""配置加载与运行时环境定位模块.

本模块是 CLI 启动主链路的「运行时依赖定位」收敛点：

- `load_config` 读取配置文件后，会把 `database.path` / `export.output_dir`
  一律解析成**以配置文件所在目录为基准的绝对路径**（而非当前工作目录）。
  这样「在哪个目录执行 CLI」与「数据落在哪」彻底解耦——归档恢复到新目录后，
  无需 `cd` 即可用 `-c <新目录>/config.json` 定位到新目录里的数据。
- 运行时状态（active_plan / operator）与数据库同目录落盘，重启后自动回填。

所有下游模块（db / merger / session_archive / archive / templates ...）统一通过
`get_db_path` / `get_export_dir` 取绝对路径，不再各自用 `os.path.abspath` 相对 CWD 解析。
"""
import copy
import json
import os
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


def _config_base_dir(config_path: Optional[str]) -> str:
    """相对数据路径的解析基准：配置文件所在目录；未提供配置文件时回退到 CWD。"""
    if not config_path:
        return os.getcwd()
    return os.path.dirname(os.path.abspath(config_path))


def _resolve_path(path: str, base_dir: str) -> str:
    """把路径解析为绝对路径；相对路径以 base_dir 为基准（而非 CWD）。"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def _resolve_runtime_paths(config: Dict[str, Any], base_dir: str) -> None:
    """就地解析 database.path / export.output_dir 为绝对路径（以 base_dir 为基准）。

    这是路径解析的唯一收敛点：相对路径一律相对配置文件所在目录，确保
    「执行目录」与「数据目录」解耦。归档恢复时写回的是相对路径
    （./audit_data/audit.db），恢复后无论从哪里执行都能正确定位。
    """
    config.setdefault("database", {})["path"] = _resolve_path(
        config.get("database", {}).get("path", "./audit_data/audit.db"), base_dir,
    )
    config.setdefault("export", {})["output_dir"] = _resolve_path(
        config.get("export", {}).get("output_dir", "./audit_data/exports"), base_dir,
    )


def load_config(config_path: str = None) -> Dict[str, Any]:
    """加载配置文件，不存在则返回默认配置，并合并运行时状态（active_plan/operator）.

    Args:
        config_path: 配置文件路径，支持 JSON 格式；可为相对或绝对路径

    Returns:
        配置字典，其中 database.path / export.output_dir 已解析为绝对路径
        （以配置文件所在目录为基准，未提供配置文件时以 CWD 为基准）

    说明：
        这是「运行时依赖定位」的入口。相对路径不再相对 CWD 解析，因此
        `-c /anywhere/config.json` 从任意目录执行都能定位到该配置同目录下的数据，
        归档恢复到新目录后无需手动 cd 或设置 PYTHONPATH 即可继续工作。
    """
    config = copy.deepcopy(DEFAULT_CONFIG)
    base_dir = _config_base_dir(config_path)

    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8-sig") as f:
            user_config = json.load(f)
        _deep_merge(config, user_config)

    _resolve_runtime_paths(config, base_dir)

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
    """获取数据库文件绝对路径（load_config 已保证为绝对路径，此处兜底）。"""
    path = config["database"]["path"]
    return os.path.abspath(path)


def get_export_dir(config: Dict[str, Any]) -> str:
    """获取导出目录绝对路径（load_config 已保证为绝对路径，此处兜底）。

    下游统一用这个访问器，避免散落的 `os.path.abspath(config['export']['output_dir'])`
    在相对路径场景下悄悄退回到 CWD 解析。
    """
    return os.path.abspath(config["export"]["output_dir"])


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
