"""操作日志回放模块 - 还原状态变更、备注、撤销、导出结果；冲突检测."""
import copy
import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db
from . import exporter
from . import reviewer


CONFLICT_KEEP = "keep"
CONFLICT_SNAPSHOT = "snapshot"
CONFLICT_ABORT = "abort"


def _diff_snapshot(diff: Dict[str, Any]) -> Dict[str, Any]:
    """取差异当前快照，用于冲突检测."""
    return {
        "id": diff.get("id"),
        "status": diff.get("status"),
        "remark": diff.get("remark") or "",
        "total_diff_qty": diff.get("total_diff_qty"),
    }


def detect_conflict(
    db_path: str,
    log_entry: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """检测回放日志与当前状态是否冲突.

    冲突规则：目标差异在操作之后又被其他方案/操作者改过时，
    或当前状态与快照不匹配，即视为冲突。

    Args:
        db_path: 数据库路径
        log_entry: operation_logs 中的一条记录

    Returns:
        None 表示无冲突；否则返回冲突详情字典
    """
    action_type = log_entry.get("action_type")
    target_id = log_entry.get("target_diff_id")
    if not target_id or action_type == "export":
        return None

    current = db.get_difference(db_path, target_id)
    if not current:
        return {
            "log_id": log_entry["id"],
            "reason": "target_missing",
            "message": f"差异 #{target_id} 不存在，无法回放 {action_type}",
            "log_entry": log_entry,
            "current": None,
        }

    snapshot_before = log_entry.get("snapshot_before") or {}
    current_snap = _diff_snapshot(current)

    if action_type == "status_change":
        expected_old = snapshot_before.get("status")
        if expected_old is not None and current_snap["status"] != expected_old:
            return {
                "log_id": log_entry["id"],
                "reason": "status_mismatch",
                "message": (
                    f"状态冲突：日志期望回放前状态为 {expected_old}，"
                    f"但当前为 {current_snap['status']}"
                ),
                "log_entry": log_entry,
                "current": current_snap,
                "snapshot_before": snapshot_before,
            }
    elif action_type == "remark_change":
        expected_old = snapshot_before.get("remark", "")
        if current_snap["remark"] != expected_old:
            return {
                "log_id": log_entry["id"],
                "reason": "remark_mismatch",
                "message": (
                    f"备注冲突：日志期望回放前备注为 '{expected_old}'，"
                    f"但当前为 '{current_snap['remark']}'"
                ),
                "log_entry": log_entry,
                "current": current_snap,
                "snapshot_before": snapshot_before,
            }
    elif action_type == "undo":
        if log_entry.get("plan_id") is not None:
            log_plan_id = log_entry["plan_id"]
            log_operator = log_entry.get("operator")
            last_history = db.get_last_review_action(db_path)
            if last_history:
                other_plan = last_history.get("plan_id")
                other_operator = last_history.get("operator")
                if (other_plan is not None and other_plan != log_plan_id) or \
                   (other_operator is not None and other_operator != log_operator):
                    return {
                        "log_id": log_entry["id"],
                        "reason": "cross_plan_undo",
                        "message": (
                            f"撤销冲突：日志由方案 '{log_entry.get('plan_name')}'/"
                            f"{log_operator} 操作，但最后一次操作来自方案 "
                            f"'{other_plan}'/{other_operator}"
                        ),
                        "log_entry": log_entry,
                        "current": current_snap,
                    }
    return None


def save_snapshot(db_path: str, output_dir: str, diff_id: int, label: str = "") -> str:
    """将差异状态另存为快照 JSON 文件.

    Returns:
        快照文件路径
    """
    os.makedirs(output_dir, exist_ok=True)
    diff = db.get_difference(db_path, diff_id)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"snapshot_diff{diff_id}_{ts}_{label or 'conflict'}.json"
    file_path = os.path.join(output_dir, filename)
    payload = {
        "diff_id": diff_id,
        "snapshot_time": ts,
        "label": label,
        "diff": _diff_snapshot(diff) if diff else None,
        "sources": diff.get("sources", []) if diff else [],
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return os.path.abspath(file_path)


def replay_operations(
    db_path: str,
    output_dir: str,
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
    operator: Optional[str] = None,
    allowed_statuses: Optional[List[str]] = None,
    default_conflict_resolution: str = CONFLICT_ABORT,
    conflict_callback: Optional[Callable[[Dict[str, Any]], str]] = None,
    action_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """按时间升序回放 operation_logs 记录.

    Args:
        db_path: 数据库路径
        output_dir: 导出与快照输出目录
        plan_id: 按方案 ID 过滤日志
        plan_name: 按方案名称过滤日志
        operator: 按操作人过滤
        allowed_statuses: 允许的状态列表
        default_conflict_resolution: 默认冲突处理策略 keep/snapshot/abort
        conflict_callback: 交互式冲突回调，返回策略字符串；None 时用默认
        action_types: 只回放指定动作类型列表（如 ["status_change", "remark_change", "export"]）；None 表示全部

    Returns:
        {"success", "replayed", "skipped", "conflicts", "aborted", "exports"}
    """
    logs = db.get_operation_logs(
        db_path, plan_id=plan_id, plan_name=plan_name, operator=operator,
    )
    if action_types:
        logs = [l for l in logs if l.get("action_type") in action_types]

    replayed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    exports: List[Dict[str, Any]] = []
    aborted: Optional[Dict[str, Any]] = None

    for log in logs:
        conflict = detect_conflict(db_path, log)
        if conflict:
            conflicts.append(conflict)
            resolution = default_conflict_resolution
            if conflict_callback:
                try:
                    resolution = conflict_callback(conflict) or default_conflict_resolution
                except Exception:
                    resolution = default_conflict_resolution

            if resolution == CONFLICT_ABORT:
                aborted = conflict
                return {
                    "success": False,
                    "replayed": replayed,
                    "skipped": skipped,
                    "conflicts": conflicts,
                    "exports": exports,
                    "aborted": aborted,
                }
            elif resolution == CONFLICT_SNAPSHOT:
                snap_path = save_snapshot(
                    db_path, output_dir, log["target_diff_id"],
                    label=f"log{log['id']}_conflict",
                )
                skipped.append({"log": log, "resolution": "snapshot", "snapshot_path": snap_path})
                continue
            else:
                skipped.append({"log": log, "resolution": "keep"})
                continue

        action_type = log.get("action_type")
        data = log.get("action_data") or {}
        log_plan_id = log.get("plan_id")
        log_plan_name = log.get("plan_name")
        log_operator = log.get("operator", "cli")

        if action_type == "status_change":
            new_status = data.get("new_status")
            if new_status is None or not log.get("target_diff_id"):
                skipped.append({"log": log, "resolution": "invalid_data"})
                continue
            r = reviewer.set_status(
                db_path, log["target_diff_id"], new_status,
                operator=log_operator,
                allowed_statuses=allowed_statuses,
                plan_id=log_plan_id, plan_name=log_plan_name,
            )
            if r.get("success"):
                replayed.append({"log_id": log["id"], "action": action_type, "result": r})
            else:
                skipped.append({"log": log, "resolution": "apply_failed", "error": r.get("error")})

        elif action_type == "remark_change":
            new_remark = data.get("new_remark", "")
            if not log.get("target_diff_id"):
                skipped.append({"log": log, "resolution": "invalid_data"})
                continue
            r = reviewer.set_remark(
                db_path, log["target_diff_id"], new_remark,
                operator=log_operator,
                plan_id=log_plan_id, plan_name=log_plan_name,
            )
            if r.get("success"):
                replayed.append({"log_id": log["id"], "action": action_type, "result": r})
            else:
                skipped.append({"log": log, "resolution": "apply_failed", "error": r.get("error")})

        elif action_type == "undo":
            r = reviewer.undo_last(
                db_path,
                operator=log_operator,
                plan_id=log_plan_id, plan_name=log_plan_name,
            )
            if r.get("success"):
                replayed.append({"log_id": log["id"], "action": action_type, "result": r})
            else:
                skipped.append({"log": log, "resolution": "apply_failed", "error": r.get("error")})

        elif action_type == "export":
            export_type = data.get("export_type")
            status_filter = data.get("status_filter")
            batch_id = data.get("batch_id")
            if export_type == "differences":
                r = exporter.export_differences(
                    db_path, output_dir, status=status_filter, include_sources=True,
                )
            elif export_type == "summary":
                r = exporter.export_summary(db_path, output_dir)
            elif export_type == "sources":
                r = exporter.export_source_lines(db_path, output_dir, batch_id=batch_id)
            else:
                skipped.append({"log": log, "resolution": "unknown_export_type"})
                continue
            if r.get("success"):
                replayed.append({"log_id": log["id"], "action": action_type, "result": r})
                exports.append(r)
            else:
                skipped.append({"log": log, "resolution": "apply_failed", "error": r.get("error")})

        else:
            skipped.append({"log": log, "resolution": "unknown_action"})

    return {
        "success": True,
        "replayed": replayed,
        "skipped": skipped,
        "conflicts": conflicts,
        "exports": exports,
        "aborted": None,
    }
