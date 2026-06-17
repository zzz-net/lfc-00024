"""复核操作模块 - 状态管理、备注、撤销功能."""
from typing import Any, Dict, List, Optional

from . import db


ALLOWED_STATUSES = {"pending", "confirmed", "ignored", "closed"}

STATUS_LABELS = {
    "pending": "待处理",
    "confirmed": "已确认",
    "ignored": "忽略",
    "closed": "已关闭",
}


def validate_status(status: str) -> bool:
    """校验状态是否合法.

    Args:
        status: 状态值

    Returns:
        是否合法
    """
    return status.lower() in ALLOWED_STATUSES


def set_status(
    db_path: str,
    diff_id: int,
    status: str,
    operator: str = "cli",
) -> Dict[str, Any]:
    """设置差异状态.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        status: 新状态
        operator: 操作人

    Returns:
        操作结果
    """
    status = status.lower()
    if not validate_status(status):
        return {
            "success": False,
            "error": f"无效状态: {status}，允许的值: {', '.join(sorted(ALLOWED_STATUSES))}",
        }

    diff = db.get_difference(db_path, diff_id)
    if not diff:
        return {
            "success": False,
            "error": f"差异不存在: {diff_id}",
        }

    old_status = diff["status"]
    if old_status == status:
        return {
            "success": True,
            "skipped": True,
            "message": f"状态已经是 {status}，无需变更",
            "diff_id": diff_id,
            "old_status": old_status,
            "new_status": status,
        }

    ok = db.update_difference_status(db_path, diff_id, status, operator)
    if not ok:
        return {
            "success": False,
            "error": "更新失败",
        }

    return {
        "success": True,
        "diff_id": diff_id,
        "old_status": old_status,
        "new_status": status,
    }


def set_remark(
    db_path: str,
    diff_id: int,
    remark: str,
    operator: str = "cli",
) -> Dict[str, Any]:
    """设置差异备注.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        remark: 备注内容
        operator: 操作人

    Returns:
        操作结果
    """
    diff = db.get_difference(db_path, diff_id)
    if not diff:
        return {
            "success": False,
            "error": f"差异不存在: {diff_id}",
        }

    old_remark = diff.get("remark") or ""
    if old_remark == remark:
        return {
            "success": True,
            "skipped": True,
            "message": "备注内容相同，无需变更",
            "diff_id": diff_id,
        }

    ok = db.update_difference_remark(db_path, diff_id, remark, operator)
    if not ok:
        return {
            "success": False,
            "error": "更新失败",
        }

    return {
        "success": True,
        "diff_id": diff_id,
        "old_remark": old_remark,
        "new_remark": remark,
    }


def batch_set_status(
    db_path: str,
    diff_ids: List[int],
    status: str,
    operator: str = "cli",
) -> Dict[str, Any]:
    """批量设置状态.

    Args:
        db_path: 数据库路径
        diff_ids: 差异 ID 列表
        status: 新状态
        operator: 操作人

    Returns:
        操作结果
    """
    status = status.lower()
    if not validate_status(status):
        return {
            "success": False,
            "error": f"无效状态: {status}",
            "updated": 0,
            "failed": [],
        }

    updated = 0
    failed: List[Dict[str, Any]] = []

    for did in diff_ids:
        result = set_status(db_path, did, status, operator)
        if result.get("success"):
            updated += 1
        else:
            failed.append({"id": did, "error": result.get("error", "未知错误")})

    return {
        "success": True,
        "updated": updated,
        "failed": failed,
        "total": len(diff_ids),
    }


def undo_last(db_path: str) -> Dict[str, Any]:
    """撤销最后一次复核操作.

    Args:
        db_path: 数据库路径

    Returns:
        撤销结果
    """
    history = db.undo_last_review(db_path)
    if not history:
        return {
            "success": False,
            "error": "没有可撤销的操作",
            "empty_history": True,
        }

    action_desc = ""
    if history["action_type"] == "status_change":
        action_desc = f"状态变更: {history['old_status']} <- {history['new_status']}"
    elif history["action_type"] == "remark_change":
        action_desc = "备注变更"

    return {
        "success": True,
        "diff_id": history["difference_id"],
        "action_type": history["action_type"],
        "description": action_desc,
        "history_id": history["id"],
    }


def get_review_history(
    db_path: str,
    diff_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """获取复核历史.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID，为 None 时查询所有
        limit: 返回条数限制

    Returns:
        历史记录列表
    """
    with db.get_conn(db_path) as conn:
        if diff_id:
            rows = conn.execute(
                """SELECT rh.*, d.location, d.sku
                   FROM review_history rh
                   JOIN differences d ON d.id = rh.difference_id
                   WHERE rh.difference_id = ?
                   ORDER BY rh.created_at DESC
                   LIMIT ?""",
                (diff_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT rh.*, d.location, d.sku
                   FROM review_history rh
                   JOIN differences d ON d.id = rh.difference_id
                   ORDER BY rh.created_at DESC
                   LIMIT ?""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_status_label(status: str) -> str:
    """获取状态的中文标签.

    Args:
        status: 状态值

    Returns:
        中文标签
    """
    return STATUS_LABELS.get(status.lower(), status)
