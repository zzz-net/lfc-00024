"""差异合并模块 - 提供合并差异的查询、统计和展示功能."""
from typing import Any, Dict, List, Optional

from . import db


def get_merged_differences(
    db_path: str,
    status: Optional[str] = None,
    location: Optional[str] = None,
    sku: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """获取合并后的差异列表，附带来源信息.

    Args:
        db_path: 数据库路径
        status: 状态过滤
        location: 库位过滤
        sku: SKU 过滤

    Returns:
        差异列表，每条包含来源行摘要
    """
    diffs = db.list_differences(db_path, status, location, sku)

    for diff in diffs:
        full = db.get_difference(db_path, diff["id"])
        if full:
            sources = full.get("sources", [])
            diff["source_count"] = len(sources)
            diff["batch_names"] = list(set(s["batch_name"] for s in sources))
            diff["line_numbers"] = [s["line_number"] for s in sources]
        else:
            diff["source_count"] = 0
            diff["batch_names"] = []
            diff["line_numbers"] = []

    return diffs


def get_diff_detail(db_path: str, diff_id: int) -> Optional[Dict[str, Any]]:
    """获取差异详情，包含完整来源行和历史记录.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID

    Returns:
        差异详情字典
    """
    return db.get_difference(db_path, diff_id)


def get_merge_summary(db_path: str) -> Dict[str, Any]:
    """获取合并统计摘要.

    Args:
        db_path: 数据库路径

    Returns:
        统计摘要字典
    """
    summary = db.get_summary(db_path)

    all_diffs = db.list_differences(db_path)
    multi_source_count = 0
    for d in all_diffs:
        detail = db.get_difference(db_path, d["id"])
        if detail and len(detail.get("sources", [])) > 1:
            multi_source_count += 1

    summary["multi_source_differences"] = multi_source_count
    summary["single_source_differences"] = summary["total_differences"] - multi_source_count

    return summary


def remerge_all(db_path: str, default_status: str = "pending") -> Dict[str, Any]:
    """重新计算所有差异（用于数据修复）.

    遍历所有来源行，重新计算差异总量和关联关系。
    不会改变已有的状态和备注。

    Args:
        db_path: 数据库路径
        default_status: 新增差异的默认状态

    Returns:
        重建结果统计
    """
    with db.get_conn(db_path) as conn:
        source_rows = conn.execute(
            """SELECT sl.*, b.batch_name
               FROM source_lines sl
               JOIN batches b ON b.id = sl.batch_id
               ORDER BY sl.id"""
        ).fetchall()

        diff_map: Dict[str, Dict[str, Any]] = {}
        source_links: Dict[str, List[int]] = {}

        for row in source_rows:
            key = f"{row['location']}|{row['sku']}"
            if key not in diff_map:
                diff_map[key] = {
                    "location": row["location"],
                    "sku": row["sku"],
                    "total_diff_qty": 0,
                }
                source_links[key] = []
            diff_map[key]["total_diff_qty"] += row["diff_qty"]
            source_links[key].append(row["id"])

        existing_diffs = conn.execute(
            "SELECT id, location, sku, status, remark FROM differences"
        ).fetchall()
        existing_map = {
            f"{r['location']}|{r['sku']}": dict(r) for r in existing_diffs
        }

        conn.execute("DELETE FROM diff_sources")
        conn.execute("DELETE FROM differences")

        created = 0
        preserved_status = 0

        for key, diff_data in diff_map.items():
            existing = existing_map.get(key)
            if existing:
                status = existing["status"]
                remark = existing["remark"]
                preserved_status += 1
            else:
                status = default_status
                remark = ""

            cursor = conn.execute(
                """INSERT INTO differences
                   (location, sku, total_diff_qty, status, remark)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    diff_data["location"],
                    diff_data["sku"],
                    diff_data["total_diff_qty"],
                    status,
                    remark,
                )
            )
            diff_id = cursor.lastrowid
            created += 1

            for source_id in source_links[key]:
                conn.execute(
                    "INSERT INTO diff_sources (difference_id, source_line_id) VALUES (?, ?)",
                    (diff_id, source_id)
                )

        conn.commit()

        return {
            "total_differences": created,
            "preserved_status": preserved_status,
            "source_lines": len(source_rows),
        }
