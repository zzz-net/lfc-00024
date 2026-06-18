"""批量任务模板管理 - 保存执行参数、环境变量白名单、导出选项、冲突策略.

与复核方案模板 (templates.py) 的区别：
- 批量任务模板面向 CLI 启动任务时的一整套参数，不直接关联执行步骤；
- 支持禁用/启用、复制、撤销最近一次变更；
- 导入时支持 --conflict abort|save-as|overwrite 三种策略；
- 变更会写历史记录，可 undo；
- 落盘到 JSON 文件，重启后从文件回补到数据库。
"""
import hashlib
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import db


logger = logging.getLogger(__name__)


VALID_CONFLICT_STRATEGIES = ("abort", "save-as", "overwrite")
BATCH_TEMPLATES_DIRNAME = "batch_templates"


def _ensure_batch_template_dir(config: Dict[str, Any]) -> str:
    """批量任务模板落盘目录（与数据库同级的 batch_templates/）."""
    plan_dir = os.path.join(
        os.path.dirname(os.path.abspath(config["database"]["path"])),
        BATCH_TEMPLATES_DIRNAME,
    )
    os.makedirs(plan_dir, exist_ok=True)
    return plan_dir


def _batch_template_file_path(config: Dict[str, Any], name: str) -> str:
    """批量任务模板 JSON 落盘路径."""
    return os.path.join(_ensure_batch_template_dir(config), f"{name}.json")


def _get_template_json_path(config: Dict[str, Any], name: str) -> str:
    """_batch_template_file_path 的别名，保持测试代码兼容."""
    return _batch_template_file_path(config, name)


def _read_json_file(file_path: str) -> Any:
    """读取 JSON 文件，兼容 UTF-8 BOM."""
    with open(file_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _normalize_execution_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规整执行参数，去除 None 值."""
    if not isinstance(params, dict):
        return {}
    return {k: v for k, v in params.items() if v is not None}


def _normalize_env_whitelist(whitelist: Optional[List[str]]) -> List[str]:
    """规整环境变量白名单."""
    if not isinstance(whitelist, list):
        return []
    return sorted({str(x).strip() for x in whitelist if isinstance(x, str) and x.strip()})


def _normalize_export_options(options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规整导出选项."""
    if not isinstance(options, dict):
        return {}
    result: Dict[str, Any] = {}
    for k, v in options.items():
        if v is not None:
            result[k] = v
    return result


def compute_batch_content_hash(
    execution_params: Optional[Dict[str, Any]],
    env_whitelist: Optional[List[str]],
    export_options: Optional[Dict[str, Any]],
    conflict_strategy: str,
) -> str:
    """计算批量任务模板的内容指纹（不含 name/description/disabled/时间戳）."""
    canonical = json.dumps({
        "execution_params": _normalize_execution_params(execution_params),
        "env_whitelist": _normalize_env_whitelist(env_whitelist),
        "export_options": _normalize_export_options(export_options),
        "conflict_strategy": conflict_strategy,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_batch_template(data: Any) -> Tuple[bool, str]:
    """校验批量任务模板结构完整性，返回 (是否合法, 错误信息)."""
    if not isinstance(data, dict):
        return False, "模板配置损坏：根结构不是 JSON 对象"

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "模板缺少必填字段：name（非空字符串）"
    if os.sep in name or "/" in name or "\\" in name:
        return False, f"模板名非法（含路径分隔符）：{name}"

    params = data.get("execution_params")
    if params is not None and not isinstance(params, dict):
        return False, "模板字段非法：execution_params 必须是对象"

    env_wl = data.get("env_whitelist")
    if env_wl is not None:
        if not isinstance(env_wl, list):
            return False, "模板字段非法：env_whitelist 必须是数组"
        for item in env_wl:
            if not isinstance(item, str) or not item.strip():
                return False, "模板 env_whitelist 含非字符串或空项"

    export_opts = data.get("export_options")
    if export_opts is not None and not isinstance(export_opts, dict):
        return False, "模板字段非法：export_options 必须是对象"

    cs = data.get("conflict_strategy")
    if cs is not None and cs not in VALID_CONFLICT_STRATEGIES:
        return False, (
            f"模板 conflict_strategy 非法：{cs}"
            f"（允许: {', '.join(VALID_CONFLICT_STRATEGIES)}）"
        )

    disabled = data.get("disabled")
    if disabled is not None and not isinstance(disabled, bool):
        return False, "模板字段非法：disabled 必须是布尔值"

    return True, ""


def _to_payload(tpl: Dict[str, Any]) -> Dict[str, Any]:
    """把数据库行转成导出/落盘用的 payload."""
    return {
        "schema_version": 1,
        "name": tpl["name"],
        "description": tpl.get("description"),
        "disabled": bool(tpl.get("disabled", False)),
        "execution_params": tpl.get("execution_params") or {},
        "env_whitelist": tpl.get("env_whitelist") or [],
        "export_options": tpl.get("export_options") or {},
        "conflict_strategy": tpl.get("conflict_strategy") or "abort",
        "content_hash": tpl.get("content_hash"),
    }


def _persist_batch_template_file(
    db_path: str, config: Dict[str, Any], name: str
) -> None:
    """把数据库中的批量任务模板冗余落盘到 batch_templates/<name>.json."""
    tpl = db.get_batch_template(db_path, name)
    if not tpl:
        return
    try:
        file_path = _batch_template_file_path(config, name)
        payload = _to_payload(tpl)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("落盘批量任务模板 %s 失败: %s", name, e)


def _remove_batch_template_file(config: Dict[str, Any], name: str) -> None:
    """删除批量任务模板的 JSON 落盘文件."""
    try:
        file_path = _batch_template_file_path(config, name)
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError as e:
        logger.warning("删除批量任务模板文件 %s 失败: %s", name, e)


def save_batch_template(
    db_path: str,
    config: Dict[str, Any],
    name: str,
    description: Optional[str] = None,
    execution_params: Optional[Dict[str, Any]] = None,
    env_whitelist: Optional[List[str]] = None,
    export_options: Optional[Dict[str, Any]] = None,
    conflict_strategy: str = "abort",
    disabled: bool = False,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """保存（新增或修改）批量任务模板.

    行为：
    - 模板不存在：创建并记录 create 历史；
    - 模板已存在且内容指纹相同：若 description/disabled 有变化则更新，
      记录 modify 历史；否则返回 unchanged；
    - 模板已存在且内容指纹不同：记录 modify 历史并更新。

    Returns:
        {"success", "action", "template_id", "message", ...}
    """
    if conflict_strategy not in VALID_CONFLICT_STRATEGIES:
        return {"success": False, "error": f"非法 conflict_strategy: {conflict_strategy}"}

    execution_params = _normalize_execution_params(execution_params)
    env_whitelist = _normalize_env_whitelist(env_whitelist)
    export_options = _normalize_export_options(export_options)

    ok, err = validate_batch_template({
        "name": name,
        "description": description,
        "disabled": disabled,
        "execution_params": execution_params,
        "env_whitelist": env_whitelist,
        "export_options": export_options,
        "conflict_strategy": conflict_strategy,
    })
    if not ok:
        return {"success": False, "error": err}

    content_hash = compute_batch_content_hash(
        execution_params, env_whitelist, export_options, conflict_strategy,
    )

    existing = db.get_batch_template(db_path, name)
    action = "create"
    snapshot_before: Optional[Dict[str, Any]] = None

    if existing:
        same_content = existing.get("content_hash") == content_hash
        same_desc = (description is None) or (description == existing.get("description"))
        same_disabled = (disabled == bool(existing.get("disabled", False)))

        if same_content and same_desc and same_disabled:
            _persist_batch_template_file(db_path, config, name)
            return {
                "success": True,
                "status": "unchanged",
                "action": "unchanged",
                "template_id": existing["id"],
                "message": f"批量任务模板 {name} 内容未变化",
            }

        action = "modify"
        snapshot_before = _to_payload(existing)
        template_id = db.save_batch_template(
            db_path, name, description, execution_params, env_whitelist,
            export_options, conflict_strategy, content_hash, disabled=disabled,
            existing_id=existing["id"],
        )
    else:
        template_id = db.save_batch_template(
            db_path, name, description, execution_params, env_whitelist,
            export_options, conflict_strategy, content_hash, disabled=disabled,
        )

    db.append_batch_template_history(
        db_path, template_id, name, snapshot_before, action, operator=operator,
    )
    _persist_batch_template_file(db_path, config, name)

    logger.info(
        "批量任务模板 %s: action=%s, id=%s, operator=%s",
        name, action, template_id, operator,
    )

    action_cn = {"create": "已创建", "modify": "已修改"}[action]
    return {
        "success": True,
        "status": "created" if action == "create" else "modified",
        "action": action,
        "template_id": template_id,
        "message": f"批量任务模板 {name} {action_cn}",
    }


def get_batch_template(
    db_path: str, config: Dict[str, Any], name: str
) -> Optional[Dict[str, Any]]:
    """获取批量任务模板（先读数据库，缺失再尝试 JSON 落盘文件并回写）."""
    tpl = db.get_batch_template(db_path, name)
    if tpl:
        return tpl
    try:
        file_path = _batch_template_file_path(config, name)
        if os.path.exists(file_path):
            data = _read_json_file(file_path)
            ok, err = validate_batch_template(data)
            if not ok:
                logger.warning("批量任务模板落盘文件 %s 校验失败: %s", name, err)
                return None
            execution_params = _normalize_execution_params(data.get("execution_params"))
            env_whitelist = _normalize_env_whitelist(data.get("env_whitelist"))
            export_options = _normalize_export_options(data.get("export_options"))
            conflict_strategy = data.get("conflict_strategy") or "abort"
            content_hash = compute_batch_content_hash(
                execution_params, env_whitelist, export_options, conflict_strategy,
            )
            db.save_batch_template(
                db_path, data["name"], data.get("description"),
                execution_params, env_whitelist, export_options,
                conflict_strategy, content_hash,
                disabled=bool(data.get("disabled", False)),
            )
            return db.get_batch_template(db_path, name)
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as e:
        logger.warning("从落盘文件恢复批量任务模板 %s 失败: %s", name, e)
        return None
    return None


def list_batch_templates(
    db_path: str, include_disabled: bool = True,
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """列出所有批量任务模板（传入 config 时会扫描 JSON 落盘回补缺失模板）."""
    if config is not None:
        try:
            bt_dir = _ensure_batch_template_dir(config)
            for fname in os.listdir(bt_dir):
                if not fname.endswith(".json"):
                    continue
                name = fname[:-5]
                if db.get_batch_template(db_path, name) is None:
                    # DB 里没有，尝试从 JSON 回补
                    file_path = os.path.join(bt_dir, fname)
                    try:
                        data = _read_json_file(file_path)
                        ok, _ = validate_batch_template(data)
                        if ok:
                            execution_params = _normalize_execution_params(
                                data.get("execution_params"))
                            env_whitelist = _normalize_env_whitelist(
                                data.get("env_whitelist"))
                            export_options = _normalize_export_options(
                                data.get("export_options"))
                            cs = data.get("conflict_strategy") or "abort"
                            content_hash = compute_batch_content_hash(
                                execution_params, env_whitelist,
                                export_options, cs,
                            )
                            db.save_batch_template(
                                db_path, data["name"], data.get("description"),
                                execution_params, env_whitelist, export_options,
                                cs, content_hash,
                                disabled=bool(data.get("disabled", False)),
                            )
                    except (OSError, json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            pass
    return db.list_batch_templates(db_path, include_disabled=include_disabled)


def delete_batch_template(
    db_path: str, config: Dict[str, Any], name: str,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """删除批量任务模板（数据库 + JSON 文件），可 undo."""
    existing = db.get_batch_template(db_path, name)
    if not existing:
        file_path = _batch_template_file_path(config, name)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
            return {"success": True, "status": "deleted", "action": "deleted_file_only",
                    "message": f"批量任务模板 {name}（仅落盘文件）已删除"}
        return {"success": False, "status": "error",
                "error": f"批量任务模板不存在：{name}"}

    snapshot_before = _to_payload(existing)
    db.append_batch_template_history(
        db_path, existing["id"], name, snapshot_before, "delete", operator=operator,
    )

    ok = db.delete_batch_template(db_path, name)
    _remove_batch_template_file(config, name)

    logger.info(
        "批量任务模板 %s 删除: id=%s, operator=%s",
        name, existing["id"], operator,
    )

    return {"success": True, "status": "deleted", "action": "deleted",
            "template_id": existing["id"],
            "message": f"批量任务模板 {name} 已删除"}


def set_batch_template_disabled(
    db_path: str, config: Dict[str, Any], name: str, disabled: bool,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """启用或禁用批量任务模板，可 undo."""
    existing = db.get_batch_template(db_path, name)
    if not existing:
        return {"success": False, "error": f"批量任务模板不存在：{name}"}

    if bool(existing.get("disabled", False)) == disabled:
        return {
            "success": True,
            "action": "unchanged",
            "template_id": existing["id"],
            "message": f"批量任务模板 {name} 已是 {'禁用' if disabled else '启用'}状态",
        }

    snapshot_before = _to_payload(existing)
    ok = db.set_batch_template_disabled(db_path, name, disabled)
    if not ok:
        return {"success": False, "error": f"更新批量任务模板失败：{name}"}

    db.append_batch_template_history(
        db_path, existing["id"], name, snapshot_before,
        "disable" if disabled else "enable", operator=operator,
    )
    _persist_batch_template_file(db_path, config, name)

    action_cn = "已禁用" if disabled else "已启用"
    logger.info(
        "批量任务模板 %s %s: id=%s, operator=%s",
        name, action_cn, existing["id"], operator,
    )

    return {
        "success": True,
        "action": "disable" if disabled else "enable",
        "template_id": existing["id"],
        "message": f"批量任务模板 {name} {action_cn}",
    }


def copy_batch_template(
    db_path: str, config: Dict[str, Any],
    src_name: str, dst_name: str,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """复制批量任务模板，dst_name 已存在时报错."""
    if src_name == dst_name:
        return {"success": False, "status": "error",
                "error": "源模板名和目标模板名不能相同"}

    src = get_batch_template(db_path, config, src_name)
    if not src:
        return {"success": False, "status": "error",
                "error": f"源批量任务模板不存在：{src_name}"}

    dst = db.get_batch_template(db_path, dst_name)
    if dst:
        return {"success": False, "status": "error",
                "error": f"目标批量任务模板已存在：{dst_name}"}

    ok, err = validate_batch_template({"name": dst_name})
    if not ok:
        return {"success": False, "status": "error", "error": err}

    result = save_batch_template(
        db_path, config, dst_name,
        description=src.get("description") or (f"复制自 {src_name}"),
        execution_params=src.get("execution_params"),
        env_whitelist=src.get("env_whitelist"),
        export_options=src.get("export_options"),
        conflict_strategy=src.get("conflict_strategy") or "abort",
        disabled=bool(src.get("disabled", False)),
        operator=operator,
    )
    if result.get("success"):
        result["status"] = "copied"
    return result


def undo_last_batch_template_change(
    db_path: str, config: Dict[str, Any], name: str,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """撤销指定批量任务模板的最近一次变更.

    支持撤销：create / modify / delete / disable / enable
    - create → 删除模板
    - modify → 恢复到 snapshot_before
    - delete → 从 snapshot_before 重建模板
    - disable / enable → 翻转 disabled 状态
    """
    last = db.get_last_batch_template_history(db_path, name)
    if not last:
        return {"success": False, "status": "not_found",
                "error": f"批量任务模板 {name} 无变更历史可撤销"}

    action = last["action"]
    snapshot = last.get("snapshot_before")
    history_id = last["id"]

    result: Dict[str, Any]
    if action == "create":
        ok = db.delete_batch_template(db_path, name)
        _remove_batch_template_file(config, name)
        result = {
            "success": ok,
            "status": "deleted" if ok else "error",
            "action": "undo_create",
            "message": f"已撤销创建：批量任务模板 {name} 已删除",
        }
    elif action == "delete":
        if not snapshot:
            result = {"success": False, "status": "error",
                      "error": "历史快照缺失，无法撤销删除"}
        else:
            content_hash = compute_batch_content_hash(
                snapshot.get("execution_params"),
                snapshot.get("env_whitelist"),
                snapshot.get("export_options"),
                snapshot.get("conflict_strategy") or "abort",
            )
            template_id = db.save_batch_template(
                db_path, snapshot["name"], snapshot.get("description"),
                _normalize_execution_params(snapshot.get("execution_params")),
                _normalize_env_whitelist(snapshot.get("env_whitelist")),
                _normalize_export_options(snapshot.get("export_options")),
                snapshot.get("conflict_strategy") or "abort",
                content_hash,
                disabled=bool(snapshot.get("disabled", False)),
            )
            _persist_batch_template_file(db_path, config, name)
            result = {
                "success": True,
                "status": "restored",
                "action": "undo_delete",
                "template_id": template_id,
                "message": f"已撤销删除：批量任务模板 {name} 已恢复",
            }
    elif action in ("modify", "disable", "enable"):
        if not snapshot:
            result = {"success": False, "status": "error",
                      "error": "历史快照缺失，无法撤销"}
        else:
            current = db.get_batch_template(db_path, name)
            if current:
                content_hash = compute_batch_content_hash(
                    snapshot.get("execution_params"),
                    snapshot.get("env_whitelist"),
                    snapshot.get("export_options"),
                    snapshot.get("conflict_strategy") or "abort",
                )
                db.save_batch_template(
                    db_path, snapshot["name"], snapshot.get("description"),
                    _normalize_execution_params(snapshot.get("execution_params")),
                    _normalize_env_whitelist(snapshot.get("env_whitelist")),
                    _normalize_export_options(snapshot.get("export_options")),
                    snapshot.get("conflict_strategy") or "abort",
                    content_hash,
                    disabled=bool(snapshot.get("disabled", False)),
                    existing_id=current["id"],
                )
                _persist_batch_template_file(db_path, config, name)
                result = {
                    "success": True,
                    "status": "restored",
                    "action": f"undo_{action}",
                    "template_id": current["id"],
                    "message": f"已撤销 {action}：批量任务模板 {name} 已恢复到变更前状态",
                }
            else:
                result = {"success": False, "status": "error",
                          "error": f"批量任务模板 {name} 不存在"}
    else:
        result = {"success": False, "status": "error",
                  "error": f"未知历史动作类型：{action}"}

    if result.get("success"):
        db.delete_batch_template_history(db_path, history_id)
        logger.info(
            "批量任务模板 %s 撤销成功: action=%s, operator=%s",
            name, action, operator,
        )
    else:
        logger.warning(
            "批量任务模板 %s 撤销失败: action=%s, reason=%s",
            name, action, result.get("error"),
        )

    return result


def export_batch_template(
    db_path: str, config: Dict[str, Any], name: str, file_path: str,
) -> Dict[str, Any]:
    """把批量任务模板导出为 JSON 配置文件."""
    tpl = get_batch_template(db_path, config, name)
    if not tpl:
        return {"success": False, "status": "error",
                "error": f"批量任务模板不存在：{name}"}
    payload = _to_payload(tpl)
    abs_path = os.path.abspath(file_path)
    try:
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.isdir(abs_path):
            return {"success": False, "status": "error",
                    "error": f"输出路径是目录而非文件：{abs_path}"}
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except PermissionError as e:
        return {"success": False, "status": "error",
                "error": f"权限不足，无法写入文件：{e}"}
    except OSError as e:
        return {"success": False, "status": "error",
                "error": f"写入模板文件失败：{e}"}

    logger.info("批量任务模板 %s 已导出: %s", name, abs_path)
    return {"success": True, "status": "exported",
            "file_path": abs_path, "template": payload}


def _find_unique_name(db_path: str, base_name: str) -> str:
    """为 save-as 策略找一个不冲突的新名称."""
    candidate = base_name
    suffix = 2
    while db.get_batch_template(db_path, candidate) is not None:
        candidate = f"{base_name}_{suffix}"
        suffix += 1
    return candidate


def import_batch_template(
    db_path: str, config: Dict[str, Any], file_path: str,
    conflict: str = "abort",
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """从 JSON 文件导入批量任务模板，按冲突策略处理重名.

    conflict 策略：
    - abort（默认）：重名且内容不同时立即中止，不改动任何数据
    - save-as：重名时自动另存为 <name>_2, <name>_3 ...
    - overwrite：重名时直接覆盖已有模板（记录 modify 历史，可 undo）
    """
    if conflict not in VALID_CONFLICT_STRATEGIES:
        raise ValueError(
            f"非法 --conflict 值：{conflict}"
            f"（允许: {', '.join(VALID_CONFLICT_STRATEGIES)}）"
        )

    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return {"success": False, "status": "error",
                "error": f"模板文件不存在：{abs_path}"}
    if not os.path.isfile(abs_path):
        return {"success": False, "status": "error",
                "error": f"模板路径不是文件：{abs_path}"}

    try:
        data = _read_json_file(abs_path)
    except PermissionError as e:
        return {"success": False, "status": "error",
                "error": f"权限不足，无法读取文件：{e}"}
    except json.JSONDecodeError as e:
        return {"success": False, "status": "error",
                "error": f"模板配置损坏（JSON 解析失败）：{e}"}
    except OSError as e:
        return {"success": False, "status": "error",
                "error": f"读取模板文件失败：{e}"}

    ok, err = validate_batch_template(data)
    if not ok:
        return {"success": False, "status": "error",
                "error": f"模板数据校验失败（missing required fields）：{err}"}

    name = data["name"]
    description = data.get("description")
    execution_params = _normalize_execution_params(data.get("execution_params"))
    env_whitelist = _normalize_env_whitelist(data.get("env_whitelist"))
    export_options = _normalize_export_options(data.get("export_options"))
    conflict_strategy = data.get("conflict_strategy") or "abort"
    disabled = bool(data.get("disabled", False))
    content_hash = compute_batch_content_hash(
        execution_params, env_whitelist, export_options, conflict_strategy,
    )

    existing = db.get_batch_template(db_path, name)
    final_name = name

    if existing:
        if existing.get("content_hash") == content_hash:
            _persist_batch_template_file(db_path, config, name)
            logger.info("批量任务模板导入 %s: 内容一致，跳过", name)
            return {
                "success": True,
                "status": "unchanged",
                "action": "unchanged",
                "template_id": existing["id"],
                "name": name,
                "resolved_name": name,
                "message": f"批量任务模板 {name} 已存在且内容一致，跳过导入",
            }

        if conflict == "abort":
            logger.info("批量任务模板导入 %s: 重名冲突，abort", name)
            return {
                "success": False,
                "status": "conflict_aborted",
                "conflict": True,
                "reason": "name_conflict",
                "existing_name": name,
                "resolved_name": name,
                "message": (
                    f"批量任务模板 {name} 已存在且内容不同，导入中止。"
                    f"使用 --conflict save-as 另存或 --conflict overwrite 覆盖"
                ),
            }
        elif conflict == "save-as":
            final_name = _find_unique_name(db_path, name)
            logger.info(
                "批量任务模板导入 %s: 重名冲突，save-as → %s", name, final_name,
            )
        elif conflict == "overwrite":
            logger.info("批量任务模板导入 %s: 重名冲突，overwrite", name)

    result = save_batch_template(
        db_path, config, final_name,
        description=description,
        execution_params=execution_params,
        env_whitelist=env_whitelist,
        export_options=export_options,
        conflict_strategy=conflict_strategy,
        disabled=disabled,
        operator=operator,
    )

    if result.get("success"):
        result["imported_from"] = abs_path
        result["resolved_name"] = final_name
        if final_name != name:
            result["saved_as"] = final_name
            result["status"] = "created"
            result["message"] = (
                f"批量任务模板 {name} 已另存为 {final_name} 并导入成功"
            )
        else:
            if result.get("action") == "modify":
                result["status"] = "overwritten"
                result["message"] = (
                    f"批量任务模板 {name} 已覆盖更新"
                )
            else:
                result["status"] = "created"
                result["message"] = (
                    f"批量任务模板 {final_name} 导入成功"
                )
    else:
        if "status" not in result:
            result["status"] = "error"

    return result
