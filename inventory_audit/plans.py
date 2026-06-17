"""复核方案管理 - 保存/加载筛选条件、导出字段、备注模板."""
import json
import os
from typing import Any, Dict, List, Optional

from . import db


DEFAULT_EXPORT_FIELDS = [
    "id", "location", "sku", "total_diff_qty", "status", "remark",
    "batch_names", "source_count", "created_at", "updated_at",
]


def _ensure_plan_dir(config: Dict[str, Any]) -> str:
    """确保方案落盘目录存在并返回路径."""
    plan_dir = os.path.join(
        os.path.dirname(os.path.abspath(config["database"]["path"])),
        "plans",
    )
    os.makedirs(plan_dir, exist_ok=True)
    return plan_dir


def _plan_file_path(config: Dict[str, Any], name: str) -> str:
    """方案 JSON 的落盘路径（冗余保存，重启后即使 DB 异常也可读）."""
    return os.path.join(_ensure_plan_dir(config), f"{name}.json")


def save_plan(
    db_path: str,
    config: Dict[str, Any],
    name: str,
    filter_status: Optional[str] = None,
    filter_location: Optional[str] = None,
    filter_sku: Optional[str] = None,
    export_fields: Optional[List[str]] = None,
    remark_template: Optional[str] = None,
) -> Dict[str, Any]:
    """保存复核方案（双写：数据库 + JSON 文件）.

    Args:
        db_path: 数据库路径
        config: 全局配置（用于计算落盘目录）
        name: 方案名称（唯一）
        filter_status: 状态过滤
        filter_location: 库位过滤
        filter_sku: SKU 过滤
        export_fields: 导出字段列表
        remark_template: 备注模板

    Returns:
        {"success", "plan_id", "plan"}
    """
    plan_id = db.save_plan(
        db_path, name,
        filter_status=filter_status,
        filter_location=filter_location,
        filter_sku=filter_sku,
        export_fields=export_fields,
        remark_template=remark_template,
    )
    plan = db.get_plan(db_path, name)
    try:
        file_path = _plan_file_path(config, name)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return {"success": True, "plan_id": plan_id, "plan": plan}


def list_plans(db_path: str) -> List[Dict[str, Any]]:
    """列出所有方案（以数据库为准）."""
    return db.list_plans(db_path)


def get_plan(db_path: str, config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """获取方案（先读数据库，缺失再尝试 JSON 落盘文件并回写）.

    这样即使数据库被重置，只要 plans/*.json 还在，方案就不丢。
    """
    plan = db.get_plan(db_path, name)
    if plan:
        return plan
    try:
        file_path = _plan_file_path(config, name)
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            plan_id = db.save_plan(
                db_path, data["name"],
                filter_status=data.get("filter_status"),
                filter_location=data.get("filter_location"),
                filter_sku=data.get("filter_sku"),
                export_fields=data.get("export_fields"),
                remark_template=data.get("remark_template"),
            )
            return db.get_plan(db_path, name)
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    return None


def delete_plan(db_path: str, config: Dict[str, Any], name: str) -> bool:
    """删除方案（数据库 + JSON 文件同时清理）."""
    ok = db.delete_plan(db_path, name)
    try:
        file_path = _plan_file_path(config, name)
        if os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass
    return ok


def apply_plan_filters(
    plan: Optional[Dict[str, Any]],
    status: Optional[str],
    location: Optional[str],
    sku: Optional[str],
) -> Dict[str, Optional[str]]:
    """将方案筛选条件与用户显式参数合并（显式参数优先级更高）.

    Args:
        plan: 当前方案，可为 None
        status: 用户显式传的 status 参数
        location: 用户显式传的 location 参数
        sku: 用户显式传的 sku 参数

    Returns:
        {"status", "location", "sku"}
    """
    result = {"status": None, "location": None, "sku": None}
    if plan:
        result["status"] = plan.get("filter_status")
        result["location"] = plan.get("filter_location")
        result["sku"] = plan.get("filter_sku")
    if status is not None:
        result["status"] = status
    if location is not None:
        result["location"] = location
    if sku is not None:
        result["sku"] = sku
    return result


def resolve_export_fields(plan: Optional[Dict[str, Any]]) -> List[str]:
    """解析实际导出字段：有方案用方案，无方案用默认."""
    if plan and plan.get("export_fields"):
        return list(plan["export_fields"])
    return list(DEFAULT_EXPORT_FIELDS)
