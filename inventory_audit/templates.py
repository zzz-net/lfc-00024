"""复核方案模板管理 - 可复用的筛选条件、导出字段、回放动作集合.

模板与临时方案 (plans) 的区别：
- 模板是版本化的、可从配置文件导入导出、支持按步骤批量执行；
- 模板内容变更会 bump 版本号，已有执行记录保留其创建时的版本，
  不会被静默改写。
"""
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import db


VALID_STEP_ACTIONS = ("list", "export", "replay")
VALID_EXPORT_TYPES = ("differences", "summary", "sources")


def _read_json_file(file_path: str) -> Any:
    """读取 JSON 文件，兼容 UTF-8 BOM（Windows 常见）.

    使用 ``utf-8-sig`` 打开，既有 BOM 时自动剥离，无 BOM 时行为等价于
    ``utf-8``。覆盖模板导入、落盘恢复、steps 文件等所有外部 JSON 输入。
    """
    with open(file_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _ensure_template_dir(config: Dict[str, Any]) -> str:
    """模板落盘目录（与数据库同级的 templates/）."""
    plan_dir = os.path.join(
        os.path.dirname(os.path.abspath(config["database"]["path"])),
        "templates",
    )
    os.makedirs(plan_dir, exist_ok=True)
    return plan_dir


def _template_file_path(config: Dict[str, Any], name: str) -> str:
    """模板 JSON 落盘路径（冗余保存，重启后即使 DB 异常也可读）."""
    return os.path.join(_ensure_template_dir(config), f"{name}.json")


def normalize_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规整筛选条件为 {status, location, sku} 三键字典."""
    if not isinstance(filters, dict):
        return {"status": None, "location": None, "sku": None}
    return {
        "status": filters.get("status") or None,
        "location": filters.get("location") or None,
        "sku": filters.get("sku") or None,
    }


def normalize_steps(steps: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """规整步骤列表，确保每个步骤是 dict 且至少有 action 键."""
    if not isinstance(steps, list):
        return []
    out: List[Dict[str, Any]] = []
    for s in steps:
        if isinstance(s, dict):
            out.append(dict(s))
    return out


def compute_content_hash(
    filters: Optional[Dict[str, Any]],
    export_fields: Optional[List[str]],
    remark_template: Optional[str],
    steps: Optional[List[Dict[str, Any]]],
) -> str:
    """计算模板可执行内容的指纹（不含 name/version/description/时间戳）.

    内容相同时指纹一致，用于检测“内容冲突”：同名但内容不同。
    """
    canonical = json.dumps({
        "filters": normalize_filters(filters),
        "export_fields": list(export_fields) if export_fields else [],
        "remark_template": remark_template or "",
        "steps": normalize_steps(steps),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_template(data: Any) -> Tuple[bool, str]:
    """校验模板结构完整性，返回 (是否合法, 错误信息).

    覆盖：配置损坏（非 dict）、字段缺失（name/steps）、步骤结构非法。
    """
    if not isinstance(data, dict):
        return False, "模板配置损坏：根结构不是 JSON 对象"

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "模板缺少必填字段：name（非空字符串）"
    if os.sep in name or "/" in name or "\\" in name:
        return False, f"模板名非法（含路径分隔符）：{name}"

    filters = data.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return False, "模板字段非法：filters 必须是对象"
    if isinstance(filters, dict):
        for k in filters:
            if k not in ("status", "location", "sku"):
                return False, f"模板 filters 含未知键：{k}"
            v = filters[k]
            if v is not None and not isinstance(v, str):
                return False, f"模板 filters.{k} 必须是字符串或 null"

    export_fields = data.get("export_fields")
    if export_fields is not None:
        if not isinstance(export_fields, list):
            return False, "模板字段非法：export_fields 必须是数组"
        for f in export_fields:
            if not isinstance(f, str) or not f.strip():
                return False, "模板 export_fields 含非字符串或空项"

    remark_template = data.get("remark_template")
    if remark_template is not None and not isinstance(remark_template, str):
        return False, "模板字段非法：remark_template 必须是字符串"

    steps = data.get("steps")
    if steps is not None:
        if not isinstance(steps, list):
            return False, "模板字段非法：steps 必须是数组"
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                return False, f"第 {i} 步损坏：不是对象"
            action = s.get("action")
            if action not in VALID_STEP_ACTIONS:
                return False, (f"第 {i} 步缺少或非法 action：{action}"
                               f"（允许: {', '.join(VALID_STEP_ACTIONS)}）")
            if action == "export":
                et = s.get("type", "differences")
                if et not in VALID_EXPORT_TYPES:
                    return False, (f"第 {i} 步 export.type 非法：{et}"
                                   f"（允许: {', '.join(VALID_EXPORT_TYPES)}）")
            if action == "replay":
                at = s.get("action_types")
                if at is not None:
                    if not isinstance(at, list) or not all(isinstance(x, str) for x in at):
                        return False, f"第 {i} 步 replay.action_types 必须是字符串数组"

    return True, ""


def save_template(
    db_path: str,
    config: Dict[str, Any],
    name: str,
    filters: Optional[Dict[str, Any]] = None,
    export_fields: Optional[List[str]] = None,
    remark_template: Optional[str] = None,
    steps: Optional[List[Dict[str, Any]]] = None,
    description: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """保存模板，处理重名与内容冲突.

    - 同名且内容指纹一致：视为无变更，不 bump 版本；
    - 同名但内容不同：内容冲突，force=False 时拒绝并返回明确提示；
      force=True 时覆盖并 bump 版本（已有执行记录保留旧版本，不被静默改写）。

    Returns:
        {"success", "action", "template_id", "version", "message", ...}
    """
    filters = normalize_filters(filters)
    steps = normalize_steps(steps)
    content_hash = compute_content_hash(filters, export_fields, remark_template, steps)

    ok, err = validate_template({
        "name": name, "filters": filters, "export_fields": export_fields,
        "remark_template": remark_template, "steps": steps,
    })
    if not ok:
        return {"success": False, "error": err}

    existing = db.get_template(db_path, name)
    if existing:
        if existing.get("content_hash") == content_hash:
            if description is not None and description != existing.get("description"):
                db.save_template(
                    db_path, name, existing["version"], description,
                    filters, export_fields, remark_template, steps, content_hash,
                )
            _persist_template_file(db_path, config, name)
            return {
                "success": True, "action": "unchanged",
                "template_id": existing["id"], "version": existing["version"],
                "message": f"模板 {name} 内容未变化，版本保持 v{existing['version']}",
            }
        if not force:
            return {
                "success": False, "conflict": True, "reason": "content_changed",
                "existing_version": existing["version"],
                "existing_hash": existing.get("content_hash"),
                "new_hash": content_hash,
                "message": (
                    f"模板 {name} 已存在（v{existing['version']}）且内容已变更，"
                    f"拒绝静默覆盖。使用 --force 显式覆盖（将 bump 版本，"
                    f"已有执行记录保留旧版本）"
                ),
            }
        new_version = int(existing.get("version", 1)) + 1
        template_id = db.save_template(
            db_path, name, new_version, description, filters,
            export_fields, remark_template, steps, content_hash,
        )
        _persist_template_file(db_path, config, name)
        return {
            "success": True, "action": "overwritten", "template_id": template_id,
            "version": new_version, "previous_version": existing["version"],
            "message": (
                f"模板 {name} 已覆盖更新至 v{new_version}（原 v{existing['version']}）；"
                f"已有执行记录保留其创建时版本，可识别版本差异"
            ),
        }

    new_version = 1
    template_id = db.save_template(
        db_path, name, new_version, description, filters,
        export_fields, remark_template, steps, content_hash,
    )
    _persist_template_file(db_path, config, name)
    return {
        "success": True, "action": "created", "template_id": template_id,
        "version": new_version, "message": f"模板 {name} 已创建（v{new_version}）",
    }


def _persist_template_file(db_path: str, config: Dict[str, Any], name: str) -> None:
    """把数据库中的模板冗余落盘到 templates/<name>.json."""
    template = db.get_template(db_path, name)
    if not template:
        return
    try:
        file_path = _template_file_path(config, name)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def get_template(db_path: str, config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """获取模板（先读数据库，缺失再尝试 JSON 落盘文件并回写）."""
    template = db.get_template(db_path, name)
    if template:
        return template
    try:
        file_path = _template_file_path(config, name)
        if os.path.exists(file_path):
            data = _read_json_file(file_path)
            ok, err = validate_template(data)
            if not ok:
                return None
            filters = normalize_filters(data.get("filters"))
            steps = normalize_steps(data.get("steps"))
            content_hash = compute_content_hash(
                filters, data.get("export_fields"),
                data.get("remark_template"), steps,
            )
            version = int(data.get("version", 1) or 1)
            db.save_template(
                db_path, data["name"], version, data.get("description"),
                filters, data.get("export_fields"),
                data.get("remark_template"), steps, content_hash,
            )
            return db.get_template(db_path, name)
    except (OSError, KeyError, json.JSONDecodeError, ValueError):
        return None
    return None


def list_templates(db_path: str) -> List[Dict[str, Any]]:
    """列出所有模板."""
    return db.list_templates(db_path)


def delete_template(db_path: str, config: Dict[str, Any], name: str) -> bool:
    """删除模板（数据库 + JSON 文件）."""
    ok = db.delete_template(db_path, name)
    try:
        file_path = _template_file_path(config, name)
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass
    return ok


def export_template(
    db_path: str,
    config: Dict[str, Any],
    name: str,
    file_path: str,
) -> Dict[str, Any]:
    """把模板导出为 JSON 配置文件（可分享/备份，再次导入可还原）."""
    template = get_template(db_path, config, name)
    if not template:
        return {"success": False, "error": f"模板不存在：{name}"}
    payload = {
        "name": template["name"],
        "version": template.get("version", 1),
        "description": template.get("description"),
        "filters": template.get("filters", {}),
        "export_fields": template.get("export_fields"),
        "remark_template": template.get("remark_template"),
        "steps": template.get("steps", []),
        "content_hash": template.get("content_hash"),
    }
    abs_path = os.path.abspath(file_path)
    try:
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return {"success": False, "error": f"写入模板文件失败：{e}"}
    return {"success": True, "file_path": abs_path, "template": payload}


def import_template(
    db_path: str,
    config: Dict[str, Any],
    file_path: str,
    force: bool = False,
) -> Dict[str, Any]:
    """从 JSON 配置文件导入模板，处理重名与内容冲突.

    - 文件损坏 / 字段缺失：返回明确错误，不导入；
    - 同名且内容一致：无变更；
    - 同名但内容不同：内容冲突，force=False 拒绝，force=True 覆盖并 bump 版本。
    """
    if not os.path.exists(file_path):
        return {"success": False, "error": f"模板文件不存在：{file_path}"}
    try:
        data = _read_json_file(file_path)
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"模板配置损坏（JSON 解析失败）：{e}"}
    except OSError as e:
        return {"success": False, "error": f"读取模板文件失败：{e}"}

    ok, err = validate_template(data)
    if not ok:
        return {"success": False, "error": err}

    name = data["name"]
    filters = normalize_filters(data.get("filters"))
    steps = normalize_steps(data.get("steps"))
    return save_template(
        db_path, config, name,
        filters=filters,
        export_fields=data.get("export_fields"),
        remark_template=data.get("remark_template"),
        steps=steps,
        description=data.get("description"),
        force=force,
    )


def check_version_drift(
    db_path: str,
    config: Dict[str, Any],
    execution: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """检查执行记录所用模板版本与当前模板版本是否漂移.

    模板被更新后，旧执行记录保留其创建时版本；此函数用于“识别版本差异”。
    返回 None 表示无漂移，否则返回漂移详情。
    """
    name = execution.get("template_name")
    if not name:
        return None
    current = get_template(db_path, config, name)
    if not current:
        return {
            "drift": True, "reason": "template_missing",
            "message": f"模板 {name} 已被删除，执行记录基于的模板已不存在",
        }
    exec_version = int(execution.get("template_version") or 0)
    cur_version = int(current.get("version") or 0)
    if exec_version != cur_version:
        return {
            "drift": True, "reason": "version_mismatch",
            "execution_version": exec_version,
            "current_version": cur_version,
            "message": (
                f"模板 {name} 已更新：执行记录基于 v{exec_version}，"
                f"当前为 v{cur_version}。续跑将使用执行记录冻结的步骤，不回写新模板"
            ),
        }
    return None
