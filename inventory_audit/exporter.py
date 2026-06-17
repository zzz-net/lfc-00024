"""报告导出模块 - 导出差异复核报告为 CSV 格式."""
import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import db
from . import merger
from . import plans as plans_mod
from . import reviewer


FIELD_LABELS = {
    "id": "差异ID",
    "location": "库位",
    "sku": "SKU",
    "total_diff_qty": "差异数量",
    "status": "状态",
    "remark": "备注",
    "batch_names": "来源批次",
    "source_count": "来源行数",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "merge_key": "合并键",
}


def _build_row(diff: Dict[str, Any], field: str) -> Any:
    """按字段名从差异对象提取导出值."""
    if field == "status":
        return reviewer.get_status_label(diff.get("status", ""))
    if field == "batch_names":
        return "; ".join(diff.get("batch_names", []))
    if field == "source_count":
        return diff.get("source_count", 0)
    return diff.get(field, "")


def export_differences(
    db_path: str,
    output_dir: str,
    status: Optional[str] = None,
    include_sources: bool = True,
    filename_prefix: str = "audit_report",
    plan: Optional[Dict[str, Any]] = None,
    operator: str = "cli",
) -> Dict[str, Any]:
    """导出差异报告为 CSV.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        status: 按状态过滤，None 表示全部（方案优先级低于显式传参）
        include_sources: 是否包含来源行明细
        filename_prefix: 文件名前缀
        plan: 当前方案，用于驱动导出字段与文件名元数据
        operator: 操作人（用于操作日志）

    Returns:
        导出结果
    """
    os.makedirs(output_dir, exist_ok=True)

    export_fields = plans_mod.resolve_export_fields(plan)

    diffs = merger.get_merged_differences(db_path, status=status)

    if not diffs:
        result = {
            "success": False,
            "error": "没有差异数据可导出",
            "file_path": None,
            "count": 0,
        }
        return result

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    status_part = f"_{status}" if status else "_all"
    plan_part = f"_plan{plan['id']}" if plan else ""
    filename = f"{filename_prefix}{status_part}{plan_part}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            "# 导出时间:", datetime.now().isoformat(timespec="seconds"),
            "方案:", plan["name"] if plan else "(无)",
            "操作人:", operator,
            "状态过滤:", status or "(全部)",
            "导出字段:", ",".join(export_fields),
        ])

        headers = [FIELD_LABELS.get(f, f) for f in export_fields]
        if include_sources:
            headers.append("来源行明细")
        writer.writerow(headers)

        for diff in diffs:
            row = [_build_row(diff, f) for f in export_fields]

            if include_sources:
                detail = db.get_difference(db_path, diff["id"])
                if detail:
                    source_details = []
                    for src in detail.get("sources", []):
                        source_details.append(
                            f"[{src['batch_name']}] 行{src['line_number']}: "
                            f"账面{src['expected_qty']} / 实盘{src['counted_qty']} "
                            f"(差异{src['diff_qty']})"
                        )
                    row.append(" | ".join(source_details))
                else:
                    row.append("")

            writer.writerow(row)

    abs_path = os.path.abspath(file_path)
    plan_id = plan["id"] if plan else None
    plan_name = plan["name"] if plan else None
    db.log_export_operation(
        db_path, "differences", abs_path, len(diffs),
        operator=operator, plan_id=plan_id, plan_name=plan_name,
        status_filter=status,
    )

    return {
        "success": True,
        "file_path": abs_path,
        "count": len(diffs),
        "filename": filename,
        "plan_id": plan_id,
        "plan_name": plan_name,
    }


def export_summary(
    db_path: str,
    output_dir: str,
    filename_prefix: str = "summary",
    plan: Optional[Dict[str, Any]] = None,
    operator: str = "cli",
) -> Dict[str, Any]:
    """导出汇总统计报告.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        filename_prefix: 文件名前缀
        plan: 当前方案（写入元数据）
        operator: 操作人

    Returns:
        导出结果
    """
    os.makedirs(output_dir, exist_ok=True)

    summary = merger.get_merge_summary(db_path)
    batches = db.list_batches(db_path)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_part = f"_plan{plan['id']}" if plan else ""
    filename = f"{filename_prefix}{plan_part}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "# 导出时间:", datetime.now().isoformat(timespec="seconds"),
            "方案:", plan["name"] if plan else "(无)",
            "操作人:", operator,
        ])

        writer.writerow(["=== 总体汇总 ==="])
        writer.writerow(["指标", "数值"])
        writer.writerow(["差异总数", summary["total_differences"]])
        writer.writerow(["差异绝对值总量", summary["total_abs_qty"]])
        writer.writerow(["批次数量", summary["batch_count"]])
        writer.writerow(["多来源差异数", summary["multi_source_differences"]])
        writer.writerow(["单来源差异数", summary["single_source_differences"]])
        writer.writerow([])

        writer.writerow(["=== 按状态统计 ==="])
        writer.writerow(["状态", "差异数", "差异绝对值数量"])
        for status, data in summary.get("by_status", {}).items():
            writer.writerow([
                reviewer.get_status_label(status),
                data["count"],
                data["qty"],
            ])
        writer.writerow([])

        writer.writerow(["=== 批次列表 ==="])
        writer.writerow(["批次ID", "批次名称", "文件路径", "导入时间", "状态"])
        for batch in batches:
            source_count = db.get_batch_source_count(db_path, batch["id"])
            writer.writerow([
                batch["id"],
                batch["batch_name"],
                batch["file_path"],
                batch["imported_at"],
                f"{batch['status']} ({source_count} 行)",
            ])

    abs_path = os.path.abspath(file_path)
    plan_id = plan["id"] if plan else None
    plan_name = plan["name"] if plan else None
    db.log_export_operation(
        db_path, "summary", abs_path, summary["total_differences"],
        operator=operator, plan_id=plan_id, plan_name=plan_name,
    )

    return {
        "success": True,
        "file_path": abs_path,
        "filename": filename,
        "summary": summary,
        "plan_id": plan_id,
        "plan_name": plan_name,
    }


def export_source_lines(
    db_path: str,
    output_dir: str,
    batch_id: Optional[int] = None,
    filename_prefix: str = "source_lines",
    plan: Optional[Dict[str, Any]] = None,
    operator: str = "cli",
) -> Dict[str, Any]:
    """导出来源行明细.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        batch_id: 批次 ID，None 表示全部
        filename_prefix: 文件名前缀
        plan: 当前方案（写入元数据）
        operator: 操作人

    Returns:
        导出结果
    """
    os.makedirs(output_dir, exist_ok=True)

    with db.get_conn(db_path) as conn:
        if batch_id:
            rows = conn.execute(
                """SELECT sl.*, b.batch_name
                   FROM source_lines sl
                   JOIN batches b ON b.id = sl.batch_id
                   WHERE sl.batch_id = ?
                   ORDER BY sl.line_number""",
                (batch_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT sl.*, b.batch_name
                   FROM source_lines sl
                   JOIN batches b ON b.id = sl.batch_id
                   ORDER BY b.imported_at, sl.line_number""",
            ).fetchall()

    if not rows:
        return {
            "success": False,
            "error": "没有来源行数据",
            "file_path": None,
            "count": 0,
        }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_part = f"_batch{batch_id}" if batch_id else "_all"
    plan_part = f"_plan{plan['id']}" if plan else ""
    filename = f"{filename_prefix}{batch_part}{plan_part}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "# 导出时间:", datetime.now().isoformat(timespec="seconds"),
            "方案:", plan["name"] if plan else "(无)",
            "操作人:", operator,
            "批次:", batch_id or "(全部)",
        ])
        writer.writerow([
            "行ID", "批次名称", "行号", "库位", "SKU",
            "账面数量", "实盘数量", "差异数量",
        ])
        for row in rows:
            writer.writerow([
                row["id"],
                row["batch_name"],
                row["line_number"],
                row["location"],
                row["sku"],
                row["expected_qty"],
                row["counted_qty"],
                row["diff_qty"],
            ])

    abs_path = os.path.abspath(file_path)
    plan_id = plan["id"] if plan else None
    plan_name = plan["name"] if plan else None
    db.log_export_operation(
        db_path, "sources", abs_path, len(rows),
        operator=operator, plan_id=plan_id, plan_name=plan_name,
        batch_id=batch_id,
    )

    return {
        "success": True,
        "file_path": abs_path,
        "count": len(rows),
        "filename": filename,
        "plan_id": plan_id,
        "plan_name": plan_name,
    }
