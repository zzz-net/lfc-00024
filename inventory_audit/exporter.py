"""报告导出模块 - 导出差异复核报告为 CSV 格式."""
import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import db
from . import merger
from . import reviewer


def export_differences(
    db_path: str,
    output_dir: str,
    status: Optional[str] = None,
    include_sources: bool = True,
    filename_prefix: str = "audit_report",
) -> Dict[str, Any]:
    """导出差异报告为 CSV.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        status: 按状态过滤，None 表示全部
        include_sources: 是否包含来源行明细
        filename_prefix: 文件名前缀

    Returns:
        导出结果
    """
    os.makedirs(output_dir, exist_ok=True)

    diffs = merger.get_merged_differences(db_path, status=status)

    if not diffs:
        return {
            "success": False,
            "error": "没有差异数据可导出",
            "file_path": None,
            "count": 0,
        }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    status_part = f"_{status}" if status else "_all"
    filename = f"{filename_prefix}{status_part}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)

        headers = [
            "差异ID",
            "库位",
            "SKU",
            "差异数量",
            "状态",
            "备注",
            "来源批次",
            "来源行数",
            "创建时间",
            "更新时间",
        ]
        if include_sources:
            headers.append("来源行明细")
        writer.writerow(headers)

        for diff in diffs:
            status_label = reviewer.get_status_label(diff["status"])
            batch_names = "; ".join(diff.get("batch_names", []))
            line_nums = ", ".join(str(n) for n in diff.get("line_numbers", []))

            row = [
                diff["id"],
                diff["location"],
                diff["sku"],
                diff["total_diff_qty"],
                status_label,
                diff.get("remark", ""),
                batch_names,
                diff.get("source_count", 0),
                diff.get("created_at", ""),
                diff.get("updated_at", ""),
            ]

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

    return {
        "success": True,
        "file_path": os.path.abspath(file_path),
        "count": len(diffs),
        "filename": filename,
    }


def export_summary(
    db_path: str,
    output_dir: str,
    filename_prefix: str = "summary",
) -> Dict[str, Any]:
    """导出汇总统计报告.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        filename_prefix: 文件名前缀

    Returns:
        导出结果
    """
    os.makedirs(output_dir, exist_ok=True)

    summary = merger.get_merge_summary(db_path)
    batches = db.list_batches(db_path)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{filename_prefix}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)

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

    return {
        "success": True,
        "file_path": os.path.abspath(file_path),
        "filename": filename,
        "summary": summary,
    }


def export_source_lines(
    db_path: str,
    output_dir: str,
    batch_id: Optional[int] = None,
    filename_prefix: str = "source_lines",
) -> Dict[str, Any]:
    """导出来源行明细.

    Args:
        db_path: 数据库路径
        output_dir: 输出目录
        batch_id: 批次 ID，None 表示全部
        filename_prefix: 文件名前缀

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
    filename = f"{filename_prefix}{batch_part}_{timestamp}.csv"
    file_path = os.path.join(output_dir, filename)

    with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
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

    return {
        "success": True,
        "file_path": os.path.abspath(file_path),
        "count": len(rows),
        "filename": filename,
    }
