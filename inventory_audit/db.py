"""数据库操作模块 - 使用 SQLite 存储批次、差异和复核历史."""
import sqlite3
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active',
    UNIQUE(file_hash)
);

CREATE TABLE IF NOT EXISTS source_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    location TEXT NOT NULL,
    sku TEXT NOT NULL,
    expected_qty REAL NOT NULL DEFAULT 0,
    counted_qty REAL NOT NULL DEFAULT 0,
    diff_qty REAL NOT NULL DEFAULT 0,
    raw_data TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(id)
);

CREATE INDEX IF NOT EXISTS idx_source_lines_batch ON source_lines(batch_id);
CREATE INDEX IF NOT EXISTS idx_source_lines_loc_sku ON source_lines(location, sku);

CREATE TABLE IF NOT EXISTS differences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location TEXT NOT NULL,
    sku TEXT NOT NULL,
    total_diff_qty REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    remark TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(location, sku)
);

CREATE INDEX IF NOT EXISTS idx_diff_status ON differences(status);

CREATE TABLE IF NOT EXISTS diff_sources (
    difference_id INTEGER NOT NULL,
    source_line_id INTEGER NOT NULL,
    PRIMARY KEY (difference_id, source_line_id),
    FOREIGN KEY (difference_id) REFERENCES differences(id),
    FOREIGN KEY (source_line_id) REFERENCES source_lines(id)
);

CREATE TABLE IF NOT EXISTS review_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    difference_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    old_remark TEXT,
    new_remark TEXT,
    operator TEXT DEFAULT 'cli',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (difference_id) REFERENCES differences(id)
);

CREATE INDEX IF NOT EXISTS idx_review_diff ON review_history(difference_id);
"""


def init_db(db_path: str) -> None:
    """初始化数据库，创建所有表.

    Args:
        db_path: 数据库文件路径
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def get_conn(db_path: str):
    """获取数据库连接上下文管理器.

    Args:
        db_path: 数据库文件路径

    Yields:
        sqlite3.Connection
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def check_batch_exists(db_path: str, file_hash: str) -> Optional[Dict[str, Any]]:
    """检查文件是否已经导入过.

    Args:
        db_path: 数据库路径
        file_hash: 文件哈希值

    Returns:
        批次信息字典，不存在返回 None
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE file_hash = ?",
            (file_hash,)
        ).fetchone()
        return dict(row) if row else None


def create_batch(
    db_path: str,
    batch_name: str,
    file_path: str,
    file_hash: str,
) -> int:
    """创建一个新批次.

    Args:
        db_path: 数据库路径
        batch_name: 批次名称
        file_path: 源文件路径
        file_hash: 文件哈希

    Returns:
        新批次 ID
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO batches (batch_name, file_path, file_hash)
               VALUES (?, ?, ?)""",
            (batch_name, file_path, file_hash)
        )
        conn.commit()
        return cursor.lastrowid


def insert_source_lines(
    db_path: str,
    batch_id: int,
    lines: List[Dict[str, Any]],
) -> List[int]:
    """批量插入来源行.

    Args:
        db_path: 数据库路径
        batch_id: 批次 ID
        lines: 来源行列表，每行包含 line_number, location, sku,
               expected_qty, counted_qty, diff_qty, raw_data

    Returns:
        插入的行 ID 列表
    """
    with get_conn(db_path) as conn:
        ids = []
        for line in lines:
            cursor = conn.execute(
                """INSERT INTO source_lines
                   (batch_id, line_number, location, sku,
                    expected_qty, counted_qty, diff_qty, raw_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    line["line_number"],
                    line["location"],
                    line["sku"],
                    line["expected_qty"],
                    line["counted_qty"],
                    line["diff_qty"],
                    line.get("raw_data", ""),
                )
            )
            ids.append(cursor.lastrowid)
        conn.commit()
        return ids


def upsert_difference(
    db_path: str,
    location: str,
    sku: str,
    diff_qty_delta: float,
    source_line_id: int,
    default_status: str = "pending",
) -> int:
    """插入或更新差异，并关联来源行.

    Args:
        db_path: 数据库路径
        location: 库位
        sku: SKU
        diff_qty_delta: 差异数量增量
        source_line_id: 来源行 ID
        default_status: 默认状态

    Returns:
        差异 ID
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, total_diff_qty FROM differences WHERE location = ? AND sku = ?",
            (location, sku)
        ).fetchone()

        if row:
            diff_id = row["id"]
            new_qty = row["total_diff_qty"] + diff_qty_delta
            conn.execute(
                """UPDATE differences
                   SET total_diff_qty = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_qty, diff_id)
            )
        else:
            cursor = conn.execute(
                """INSERT INTO differences
                   (location, sku, total_diff_qty, status)
                   VALUES (?, ?, ?, ?)""",
                (location, sku, diff_qty_delta, default_status)
            )
            diff_id = cursor.lastrowid

        try:
            conn.execute(
                "INSERT INTO diff_sources (difference_id, source_line_id) VALUES (?, ?)",
                (diff_id, source_line_id)
            )
        except sqlite3.IntegrityError:
            pass

        conn.commit()
        return diff_id


def list_differences(
    db_path: str,
    status: Optional[str] = None,
    location: Optional[str] = None,
    sku: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查询差异列表.

    Args:
        db_path: 数据库路径
        status: 按状态过滤
        location: 按库位过滤
        sku: 按 SKU 过滤

    Returns:
        差异列表
    """
    query = "SELECT * FROM differences WHERE 1=1"
    params: List[Any] = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if location:
        query += " AND location LIKE ?"
        params.append(f"%{location}%")
    if sku:
        query += " AND sku LIKE ?"
        params.append(f"%{sku}%")

    query += " ORDER BY location, sku"

    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_difference(db_path: str, diff_id: int) -> Optional[Dict[str, Any]]:
    """获取单个差异详情.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID

    Returns:
        差异详情字典，包含来源行信息
    """
    with get_conn(db_path) as conn:
        diff_row = conn.execute(
            "SELECT * FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        if not diff_row:
            return None

        diff = dict(diff_row)

        source_rows = conn.execute(
            """SELECT sl.*, b.batch_name, b.file_path
               FROM source_lines sl
               JOIN diff_sources ds ON ds.source_line_id = sl.id
               JOIN batches b ON b.id = sl.batch_id
               WHERE ds.difference_id = ?
               ORDER BY b.imported_at, sl.line_number""",
            (diff_id,)
        ).fetchall()
        diff["sources"] = [dict(r) for r in source_rows]

        history_rows = conn.execute(
            "SELECT * FROM review_history WHERE difference_id = ? ORDER BY created_at DESC",
            (diff_id,)
        ).fetchall()
        diff["history"] = [dict(r) for r in history_rows]

        return diff


def update_difference_status(
    db_path: str,
    diff_id: int,
    new_status: str,
    operator: str = "cli",
) -> bool:
    """更新差异状态，并记录历史.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        new_status: 新状态
        operator: 操作人

    Returns:
        是否成功
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        if not row:
            return False

        old_status = row["status"]
        if old_status == new_status:
            return True

        conn.execute(
            """UPDATE differences
               SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (new_status, diff_id)
        )
        conn.execute(
            """INSERT INTO review_history
               (difference_id, action_type, old_status, new_status)
               VALUES (?, 'status_change', ?, ?)""",
            (diff_id, old_status, new_status)
        )
        conn.commit()
        return True


def update_difference_remark(
    db_path: str,
    diff_id: int,
    new_remark: str,
    operator: str = "cli",
) -> bool:
    """更新差异备注，并记录历史.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        new_remark: 新备注
        operator: 操作人

    Returns:
        是否成功
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT remark FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        if not row:
            return False

        old_remark = row["remark"] or ""
        if old_remark == new_remark:
            return True

        conn.execute(
            """UPDATE differences
               SET remark = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (new_remark, diff_id)
        )
        conn.execute(
            """INSERT INTO review_history
               (difference_id, action_type, old_remark, new_remark)
               VALUES (?, 'remark_change', ?, ?)""",
            (diff_id, old_remark, new_remark)
        )
        conn.commit()
        return True


def get_last_review_action(db_path: str) -> Optional[Dict[str, Any]]:
    """获取最后一条复核历史记录.

    Args:
        db_path: 数据库路径

    Returns:
        历史记录字典
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM review_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def undo_last_review(db_path: str) -> Optional[Dict[str, Any]]:
    """撤销最后一次复核操作.

    Args:
        db_path: 数据库路径

    Returns:
        被撤销的操作信息，无可撤销时返回 None
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM review_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None

        history = dict(row)
        diff_id = history["difference_id"]

        if history["action_type"] == "status_change":
            conn.execute(
                "UPDATE differences SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (history["old_status"], diff_id)
            )
        elif history["action_type"] == "remark_change":
            conn.execute(
                "UPDATE differences SET remark = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (history["old_remark"], diff_id)
            )

        conn.execute("DELETE FROM review_history WHERE id = ?", (history["id"],))
        conn.commit()
        return history


def list_batches(db_path: str) -> List[Dict[str, Any]]:
    """列出所有批次.

    Args:
        db_path: 数据库路径

    Returns:
        批次列表
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM batches ORDER BY imported_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_summary(db_path: str) -> Dict[str, Any]:
    """获取汇总统计.

    Args:
        db_path: 数据库路径

    Returns:
        统计数据字典
    """
    with get_conn(db_path) as conn:
        total_diff = conn.execute(
            "SELECT COUNT(*) as cnt, SUM(ABS(total_diff_qty)) as qty FROM differences"
        ).fetchone()

        by_status = conn.execute(
            """SELECT status, COUNT(*) as cnt, SUM(ABS(total_diff_qty)) as qty
               FROM differences GROUP BY status"""
        ).fetchall()

        batch_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM batches"
        ).fetchone()

        result = {
            "total_differences": total_diff["cnt"] or 0,
            "total_abs_qty": total_diff["qty"] or 0,
            "batch_count": batch_count["cnt"] or 0,
            "by_status": {r["status"]: {"count": r["cnt"], "qty": r["qty"] or 0} for r in by_status},
        }
        return result


def get_batch_source_count(db_path: str, batch_id: int) -> int:
    """获取批次的来源行数量.

    Args:
        db_path: 数据库路径
        batch_id: 批次 ID

    Returns:
        来源行数量
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM source_lines WHERE batch_id = ?",
            (batch_id,)
        ).fetchone()
        return row["cnt"] if row else 0
