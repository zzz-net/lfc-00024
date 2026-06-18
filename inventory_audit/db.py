"""数据库操作模块 - 使用 SQLite 存储批次、差异和复核历史."""
import json
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
    merge_key TEXT NOT NULL,
    total_diff_qty REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    remark TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(merge_key)
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
    plan_id INTEGER,
    plan_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (difference_id) REFERENCES differences(id)
);

CREATE INDEX IF NOT EXISTS idx_review_diff ON review_history(difference_id);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    filter_status TEXT,
    filter_location TEXT,
    filter_sku TEXT,
    export_fields TEXT,
    remark_template TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    plan_name TEXT,
    operator TEXT DEFAULT 'cli',
    action_type TEXT NOT NULL,
    target_diff_id INTEGER,
    action_data TEXT,
    snapshot_before TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_op_log_plan ON operation_logs(plan_id);
CREATE INDEX IF NOT EXISTS idx_op_log_created ON operation_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_op_log_target ON operation_logs(target_diff_id);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    filters TEXT,
    export_fields TEXT,
    remark_template TEXT,
    steps TEXT,
    content_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_task_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    disabled INTEGER NOT NULL DEFAULT 0,
    execution_params TEXT,
    env_whitelist TEXT,
    export_options TEXT,
    conflict_strategy TEXT DEFAULT 'abort',
    content_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_template_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER,
    template_name TEXT NOT NULL,
    snapshot_before TEXT,
    action TEXT NOT NULL,
    operator TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES batch_task_templates(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS template_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER,
    template_name TEXT,
    template_version INTEGER,
    template_snapshot TEXT,
    operator TEXT,
    active_plan TEXT,
    status TEXT DEFAULT 'running',
    steps_total INTEGER NOT NULL DEFAULT 0,
    steps_done INTEGER NOT NULL DEFAULT 0,
    steps_failed INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_tpl_exec_tpl ON template_executions(template_id);
CREATE INDEX IF NOT EXISTS idx_tpl_exec_status ON template_executions(status);

CREATE TABLE IF NOT EXISTS template_execution_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    step TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    error TEXT,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    UNIQUE(execution_id, step_index),
    FOREIGN KEY (execution_id) REFERENCES template_executions(id)
);

CREATE INDEX IF NOT EXISTS idx_tpl_step_exec ON template_execution_steps(execution_id);
"""

TEMPLATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_tpl_name ON templates(name);
"""

INDEX_MERGE_KEY_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_diff_merge_key ON differences(merge_key);"
)


POST_MIGRATION_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_review_plan ON review_history(plan_id);
CREATE INDEX IF NOT EXISTS idx_review_created ON review_history(created_at);
"""


def init_db(db_path: str) -> None:
    """初始化数据库，创建所有表并执行必要迁移.

    迁移顺序：先建表（IF NOT EXISTS），再迁移旧表补 merge_key 列，
    再补 review_history 的 plan_id / plan_name 列，
    最后建依赖新列的索引，避免旧库因列不存在而报错。

    Args:
        db_path: 数据库文件路径
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_differences_table(conn)
        _migrate_review_history_table(conn)
        _migrate_template_executions_table(conn)
        conn.execute(INDEX_MERGE_KEY_SQL)
        conn.executescript(POST_MIGRATION_INDEX_SQL)
        conn.execute(TEMPLATE_INDEX_SQL)
        conn.commit()


def _migrate_review_history_table(conn) -> None:
    """迁移旧版 review_history 表：补充 plan_id 和 plan_name 列."""
    cols = conn.execute("PRAGMA table_info(review_history)").fetchall()
    col_names = {c["name"] for c in cols}
    if "plan_id" not in col_names:
        conn.execute("ALTER TABLE review_history ADD COLUMN plan_id INTEGER")
    if "plan_name" not in col_names:
        conn.execute("ALTER TABLE review_history ADD COLUMN plan_name TEXT")


def _migrate_template_executions_table(conn) -> None:
    """迁移旧版 template_executions 表：补充 template_snapshot / operator / active_plan 列."""
    cols = conn.execute("PRAGMA table_info(template_executions)").fetchall()
    col_names = {c["name"] for c in cols}
    if "template_snapshot" not in col_names:
        conn.execute(
            "ALTER TABLE template_executions ADD COLUMN template_snapshot TEXT"
        )
    if "operator" not in col_names:
        conn.execute(
            "ALTER TABLE template_executions ADD COLUMN operator TEXT"
        )
    if "active_plan" not in col_names:
        conn.execute(
            "ALTER TABLE template_executions ADD COLUMN active_plan TEXT"
        )


def _migrate_differences_table(conn) -> None:
    """迁移旧版 differences 表：补充 merge_key 列并切换唯一约束.

    旧表以 UNIQUE(location, sku) 作为合并键，新版改为 UNIQUE(merge_key)，
    由配置中的 rules.merge_keys 驱动。迁移时用 location|sku 回填 merge_key，
    不破坏已有批次、状态和备注。
    """
    cols = conn.execute("PRAGMA table_info(differences)").fetchall()
    col_names = {c["name"] for c in cols}
    if "merge_key" in col_names:
        return

    conn.execute("PRAGMA foreign_keys = OFF")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS differences_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            sku TEXT NOT NULL,
            merge_key TEXT NOT NULL,
            total_diff_qty REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            remark TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(merge_key)
        );
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO differences_new
            (id, location, sku, merge_key, total_diff_qty, status, remark, created_at, updated_at)
        SELECT id, location, sku,
               location || '|' || sku,
               total_diff_qty, status, remark, created_at, updated_at
        FROM differences
        """
    )
    conn.execute("DROP TABLE IF EXISTS differences")
    conn.execute("ALTER TABLE differences_new RENAME TO differences")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diff_status ON differences(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diff_merge_key ON differences(merge_key)")

    conn.execute("PRAGMA foreign_keys = ON")


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
    merge_key: str,
    diff_qty_delta: float,
    source_line_id: int,
    default_status: str = "pending",
) -> int:
    """插入或更新差异，并关联来源行.

    合并键由配置 rules.merge_keys 驱动，经调用方计算为 merge_key 传入，
    不再硬编码 location+sku。

    Args:
        db_path: 数据库路径
        location: 库位（用于展示）
        sku: SKU（用于展示）
        merge_key: 合并键（由 rules.merge_keys 计算）
        diff_qty_delta: 差异数量增量
        source_line_id: 来源行 ID
        default_status: 默认状态

    Returns:
        差异 ID
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT id, total_diff_qty FROM differences WHERE merge_key = ?",
            (merge_key,)
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
                   (location, sku, merge_key, total_diff_qty, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (location, sku, merge_key, diff_qty_delta, default_status)
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
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
) -> bool:
    """更新差异状态，并记录历史.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        new_status: 新状态
        operator: 操作人
        plan_id: 方案 ID
        plan_name: 方案名称

    Returns:
        是否成功
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, remark FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        if not row:
            return False

        old_status = row["status"]
        if old_status == new_status:
            return True

        snapshot_before = json.dumps({"status": old_status, "remark": row["remark"] or ""}, ensure_ascii=False)

        conn.execute(
            """UPDATE differences
               SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (new_status, diff_id)
        )
        conn.execute(
            """INSERT INTO review_history
               (difference_id, action_type, old_status, new_status, operator, plan_id, plan_name)
               VALUES (?, 'status_change', ?, ?, ?, ?, ?)""",
            (diff_id, old_status, new_status, operator, plan_id, plan_name)
        )
        action_data = json.dumps({"old_status": old_status, "new_status": new_status}, ensure_ascii=False)
        conn.execute(
            """INSERT INTO operation_logs
               (plan_id, plan_name, operator, action_type, target_diff_id, action_data, snapshot_before)
               VALUES (?, ?, ?, 'status_change', ?, ?, ?)""",
            (plan_id, plan_name, operator, diff_id, action_data, snapshot_before)
        )
        conn.commit()
        return True


def update_difference_remark(
    db_path: str,
    diff_id: int,
    new_remark: str,
    operator: str = "cli",
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
) -> bool:
    """更新差异备注，并记录历史.

    Args:
        db_path: 数据库路径
        diff_id: 差异 ID
        new_remark: 新备注
        operator: 操作人
        plan_id: 方案 ID
        plan_name: 方案名称

    Returns:
        是否成功
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT status, remark FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        if not row:
            return False

        old_remark = row["remark"] or ""
        if old_remark == new_remark:
            return True

        snapshot_before = json.dumps({"status": row["status"], "remark": old_remark}, ensure_ascii=False)

        conn.execute(
            """UPDATE differences
               SET remark = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (new_remark, diff_id)
        )
        conn.execute(
            """INSERT INTO review_history
               (difference_id, action_type, old_remark, new_remark, operator, plan_id, plan_name)
               VALUES (?, 'remark_change', ?, ?, ?, ?, ?)""",
            (diff_id, old_remark, new_remark, operator, plan_id, plan_name)
        )
        action_data = json.dumps({"old_remark": old_remark, "new_remark": new_remark}, ensure_ascii=False)
        conn.execute(
            """INSERT INTO operation_logs
               (plan_id, plan_name, operator, action_type, target_diff_id, action_data, snapshot_before)
               VALUES (?, ?, ?, 'remark_change', ?, ?, ?)""",
            (plan_id, plan_name, operator, diff_id, action_data, snapshot_before)
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


def undo_last_review(
    db_path: str,
    operator: str = "cli",
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """撤销最后一次复核操作.

    状态变更撤销时完整恢复 old_status；若历史记录缺失 old_status（异常情况），
    回退到 pending 而非 NULL，避免产生脏状态。备注变更同理回退到空串。

    Args:
        db_path: 数据库路径
        operator: 操作人
        plan_id: 方案 ID
        plan_name: 方案名称

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

        diff_row = conn.execute(
            "SELECT status, remark FROM differences WHERE id = ?",
            (diff_id,)
        ).fetchone()
        snapshot_before = None
        if diff_row:
            snapshot_before = json.dumps({"status": diff_row["status"], "remark": diff_row["remark"] or ""}, ensure_ascii=False)

        if history["action_type"] == "status_change":
            restore_status = history["old_status"] or "pending"
            conn.execute(
                "UPDATE differences SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (restore_status, diff_id)
            )
        elif history["action_type"] == "remark_change":
            restore_remark = history["old_remark"] if history["old_remark"] is not None else ""
            conn.execute(
                "UPDATE differences SET remark = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (restore_remark, diff_id)
            )

        conn.execute("DELETE FROM review_history WHERE id = ?", (history["id"],))

        action_data = json.dumps({
            "undone_history_id": history["id"],
            "undone_action_type": history["action_type"],
            "difference_id": diff_id,
        }, ensure_ascii=False)
        conn.execute(
            """INSERT INTO operation_logs
               (plan_id, plan_name, operator, action_type, target_diff_id, action_data, snapshot_before)
               VALUES (?, ?, ?, 'undo', ?, ?, ?)""",
            (plan_id, plan_name, operator, diff_id, action_data, snapshot_before)
        )
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


def save_plan(
    db_path: str,
    name: str,
    filter_status: Optional[str] = None,
    filter_location: Optional[str] = None,
    filter_sku: Optional[str] = None,
    export_fields: Optional[List[str]] = None,
    remark_template: Optional[str] = None,
) -> int:
    """保存或更新复核方案.

    Args:
        db_path: 数据库路径
        name: 方案名称（唯一）
        filter_status: 状态过滤值
        filter_location: 库位过滤值
        filter_sku: SKU 过滤值
        export_fields: 导出字段列表
        remark_template: 备注模板

    Returns:
        方案 ID
    """
    export_fields_json = json.dumps(export_fields, ensure_ascii=False) if export_fields else None
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT id FROM plans WHERE name = ?", (name,)).fetchone()
        if row:
            conn.execute(
                """UPDATE plans SET
                   filter_status = ?, filter_location = ?, filter_sku = ?,
                   export_fields = ?, remark_template = ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (filter_status, filter_location, filter_sku,
                 export_fields_json, remark_template, row["id"])
            )
            conn.commit()
            return row["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO plans
                   (name, filter_status, filter_location, filter_sku,
                    export_fields, remark_template)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, filter_status, filter_location, filter_sku,
                 export_fields_json, remark_template)
            )
            conn.commit()
            return cursor.lastrowid


def list_plans(db_path: str) -> List[Dict[str, Any]]:
    """列出所有方案.

    Args:
        db_path: 数据库路径

    Returns:
        方案列表
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM plans ORDER BY updated_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("export_fields"):
                d["export_fields"] = json.loads(d["export_fields"])
            result.append(d)
        return result


def get_plan(db_path: str, name: str) -> Optional[Dict[str, Any]]:
    """按名称获取方案.

    Args:
        db_path: 数据库路径
        name: 方案名称

    Returns:
        方案字典，不存在返回 None
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("export_fields"):
            d["export_fields"] = json.loads(d["export_fields"])
        return d


def delete_plan(db_path: str, name: str) -> bool:
    """删除方案.

    Args:
        db_path: 数据库路径
        name: 方案名称

    Returns:
        是否删除成功
    """
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM plans WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0


def log_export_operation(
    db_path: str,
    export_type: str,
    file_path: str,
    count: int,
    operator: str = "cli",
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
    status_filter: Optional[str] = None,
    location_filter: Optional[str] = None,
    sku_filter: Optional[str] = None,
    batch_id: Optional[int] = None,
    export_fields: Optional[List[str]] = None,
    template_id: Optional[int] = None,
    template_name: Optional[str] = None,
    template_version: Optional[int] = None,
) -> None:
    """记录导出操作到 operation_logs.

    Args:
        db_path: 数据库路径
        export_type: 导出类型 (differences/summary/sources)
        file_path: 导出文件路径
        count: 导出记录数
        operator: 操作人
        plan_id: 方案 ID
        plan_name: 方案名称
        status_filter: 状态过滤
        location_filter: 库位过滤
        sku_filter: SKU 过滤
        batch_id: 批次 ID
        export_fields: 导出字段列表（仅 differences 类型有）
        template_id: 模板 ID（批量执行时记录）
        template_name: 模板名称
        template_version: 模板版本
    """
    action_data = json.dumps({
        "export_type": export_type,
        "file_path": file_path,
        "count": count,
        "status_filter": status_filter,
        "location_filter": location_filter,
        "sku_filter": sku_filter,
        "batch_id": batch_id,
        "export_fields": export_fields,
        "template_id": template_id,
        "template_name": template_name,
        "template_version": template_version,
    }, ensure_ascii=False)
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO operation_logs
               (plan_id, plan_name, operator, action_type, target_diff_id, action_data)
               VALUES (?, ?, ?, 'export', ?, ?)""",
            (plan_id, plan_name, operator, None, action_data)
        )
        conn.commit()


def restore_operation_log(
    db_path: str,
    plan_id: Optional[int],
    plan_name: Optional[str],
    operator: Optional[str],
    action_type: str,
    target_diff_id: Optional[int],
    action_data: Dict[str, Any],
    snapshot_before: Optional[str] = None,
    created_at: Optional[str] = None,
) -> int:
    """低层次恢复一条 operation_log（用于归档恢复）.

    与 log_* 不同，不做业务约束校验，直接按提供的字段落库。
    plan_id/target_diff_id 在新库中可能不存在，调用方负责置为 None 避免外键问题。
    支持指定 created_at 以还原时间顺序。

    Returns: 新插入的 log id
    """
    action_json = json.dumps(action_data, ensure_ascii=False)
    with get_conn(db_path) as conn:
        if created_at:
            cursor = conn.execute(
                """INSERT INTO operation_logs
                   (plan_id, plan_name, operator, action_type, target_diff_id,
                    action_data, snapshot_before, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, plan_name, operator, action_type, target_diff_id,
                 action_json, snapshot_before, created_at),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO operation_logs
                   (plan_id, plan_name, operator, action_type, target_diff_id,
                    action_data, snapshot_before)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, plan_name, operator, action_type, target_diff_id,
                 action_json, snapshot_before),
            )
        conn.commit()
        return int(cursor.lastrowid)


def get_operation_logs(
    db_path: str,
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
    operator: Optional[str] = None,
    action_type: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查询操作日志（用于回放）.

    Args:
        db_path: 数据库路径
        plan_id: 按方案 ID 过滤
        plan_name: 按方案名称过滤
        operator: 按操作人过滤
        action_type: 按动作类型过滤
        limit: 条数限制

    Returns:
        操作日志列表，按时间升序
    """
    query = "SELECT * FROM operation_logs WHERE 1=1"
    params: List[Any] = []
    if plan_id is not None:
        query += " AND plan_id = ?"
        params.append(plan_id)
    if plan_name is not None:
        query += " AND plan_name = ?"
        params.append(plan_name)
    if operator is not None:
        query += " AND operator = ?"
        params.append(operator)
    if action_type is not None:
        query += " AND action_type = ?"
        params.append(action_type)
    query += " ORDER BY created_at ASC, id ASC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("action_data"):
                d["action_data"] = json.loads(d["action_data"])
            if d.get("snapshot_before"):
                d["snapshot_before"] = json.loads(d["snapshot_before"])
            result.append(d)
        return result


# ---------------------------------------------------------------------------
# 复核方案模板 (templates) 与批量执行记录
# ---------------------------------------------------------------------------

def save_template(
    db_path: str,
    name: str,
    version: int,
    description: Optional[str],
    filters: Dict[str, Any],
    export_fields: Optional[List[str]],
    remark_template: Optional[str],
    steps: List[Dict[str, Any]],
    content_hash: str,
) -> int:
    """保存或更新复核方案模板（按 name 唯一）.

    Args:
        db_path: 数据库路径
        name: 模板名称（唯一）
        version: 模板版本号
        description: 模板描述
        filters: 筛选条件 {status, location, sku}
        export_fields: 导出字段列表
        remark_template: 备注模板
        steps: 批量执行步骤列表
        content_hash: 内容指纹（用于冲突检测）

    Returns:
        模板 ID
    """
    filters_json = json.dumps(filters, ensure_ascii=False)
    fields_json = json.dumps(export_fields, ensure_ascii=False) if export_fields else None
    steps_json = json.dumps(steps, ensure_ascii=False)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT id FROM templates WHERE name = ?", (name,)).fetchone()
        if row:
            conn.execute(
                """UPDATE templates SET
                   version = ?, description = ?, filters = ?,
                   export_fields = ?, remark_template = ?, steps = ?,
                   content_hash = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (version, description, filters_json, fields_json,
                 remark_template, steps_json, content_hash, row["id"]),
            )
            conn.commit()
            return row["id"]
        cursor = conn.execute(
            """INSERT INTO templates
               (name, version, description, filters, export_fields,
                remark_template, steps, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, version, description, filters_json, fields_json,
             remark_template, steps_json, content_hash),
        )
        conn.commit()
        return cursor.lastrowid


def _row_to_template(row) -> Dict[str, Any]:
    d = dict(row)
    if d.get("filters"):
        d["filters"] = json.loads(d["filters"])
    else:
        d["filters"] = {}
    if d.get("export_fields"):
        d["export_fields"] = json.loads(d["export_fields"])
    if d.get("steps"):
        d["steps"] = json.loads(d["steps"])
    else:
        d["steps"] = []
    return d


def get_template(db_path: str, name: str) -> Optional[Dict[str, Any]]:
    """按名称获取模板."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_template(row) if row else None


def get_template_by_id(db_path: str, template_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 获取模板."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        return _row_to_template(row) if row else None


def list_templates(db_path: str) -> List[Dict[str, Any]]:
    """列出所有模板（按更新时间倒序）."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM templates ORDER BY updated_at DESC"
        ).fetchall()
        return [_row_to_template(r) for r in rows]


def delete_template(db_path: str, name: str) -> bool:
    """删除模板."""
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM templates WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0


def create_execution(
    db_path: str,
    template_id: Optional[int],
    template_name: str,
    template_version: int,
    steps_total: int,
    template_snapshot: Optional[Dict[str, Any]] = None,
    operator: Optional[str] = None,
    active_plan: Optional[str] = None,
) -> int:
    """创建一条批量执行记录，冻结模板快照，返回 execution_id."""
    snapshot_json = json.dumps(template_snapshot, ensure_ascii=False) if template_snapshot else None
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO template_executions
               (template_id, template_name, template_version,
                template_snapshot, operator, active_plan,
                status, steps_total, steps_done, steps_failed)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?, 0, 0)""",
            (template_id, template_name, template_version, snapshot_json,
             operator, active_plan, steps_total),
        )
        conn.commit()
        return cursor.lastrowid


def get_execution(db_path: str, execution_id: int) -> Optional[Dict[str, Any]]:
    """获取执行记录（含冻结的模板快照）."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM template_executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("template_snapshot"):
            d["template_snapshot"] = json.loads(d["template_snapshot"])
        return d


def list_executions(
    db_path: str,
    template_id: Optional[int] = None,
    template_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """列出执行记录（按开始时间倒序）."""
    query = "SELECT * FROM template_executions WHERE 1=1"
    params: List[Any] = []
    if template_id is not None:
        query += " AND template_id = ?"
        params.append(template_id)
    if template_name is not None:
        query += " AND template_name = ?"
        params.append(template_name)
    query += " ORDER BY started_at DESC, id DESC"
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def update_execution(
    db_path: str,
    execution_id: int,
    status: Optional[str] = None,
    steps_done: Optional[int] = None,
    steps_failed: Optional[int] = None,
    finished: bool = False,
) -> None:
    """更新执行记录状态."""
    with get_conn(db_path) as conn:
        sets = []
        params: List[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if steps_done is not None:
            sets.append("steps_done = ?")
            params.append(steps_done)
        if steps_failed is not None:
            sets.append("steps_failed = ?")
            params.append(steps_failed)
        if finished:
            sets.append("finished_at = CURRENT_TIMESTAMP")
        if not sets:
            return
        params.append(execution_id)
        conn.execute(
            f"UPDATE template_executions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        conn.commit()


def upsert_step(
    db_path: str,
    execution_id: int,
    step_index: int,
    step: Dict[str, Any],
    status: str,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    started: bool = False,
    finished: bool = False,
) -> None:
    """插入或更新某一步的执行状态（UNIQUE(execution_id, step_index) 保证幂等）."""
    step_json = json.dumps(step, ensure_ascii=False)
    result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM template_execution_steps "
            "WHERE execution_id = ? AND step_index = ?",
            (execution_id, step_index),
        ).fetchone()
        if existing:
            sets = ["status = ?", "result = ?", "error = ?"]
            params: List[Any] = [status, result_json, error]
            if started:
                sets.append("started_at = CURRENT_TIMESTAMP")
            if finished:
                sets.append("finished_at = CURRENT_TIMESTAMP")
            params += [existing["id"]]
            conn.execute(
                f"UPDATE template_execution_steps SET {', '.join(sets)} "
                "WHERE id = ?",
                params,
            )
        else:
            conn.execute(
                """INSERT INTO template_execution_steps
                   (execution_id, step_index, step, status, result, error,
                    started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?,
                           CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (execution_id, step_index, step_json, status, result_json, error),
            )
        conn.commit()


def get_step(
    db_path: str,
    execution_id: int,
    step_index: int,
) -> Optional[Dict[str, Any]]:
    """获取某一步的执行记录."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM template_execution_steps "
            "WHERE execution_id = ? AND step_index = ?",
            (execution_id, step_index),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("step"):
            d["step"] = json.loads(d["step"])
        if d.get("result"):
            d["result"] = json.loads(d["result"])
        return d


def get_steps(db_path: str, execution_id: int) -> List[Dict[str, Any]]:
    """获取某次执行的所有步骤记录（按 step_index 升序）."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM template_execution_steps "
            "WHERE execution_id = ? ORDER BY step_index ASC",
            (execution_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("step"):
                d["step"] = json.loads(d["step"])
            if d.get("result"):
                d["result"] = json.loads(d["result"])
            result.append(d)
        return result


# ============================================================================
# 批量任务模板 (batch_task_templates)
# ============================================================================

VALID_CONFLICT_STRATEGIES = ("abort", "save-as", "overwrite")


def _row_to_batch_template(row) -> Dict[str, Any]:
    d = dict(row)
    for key in ("execution_params", "env_whitelist", "export_options"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key] = None
    d["disabled"] = bool(d.get("disabled", 0))
    return d


def save_batch_template(
    db_path: str,
    name: str,
    description: Optional[str],
    execution_params: Optional[Dict[str, Any]],
    env_whitelist: Optional[List[str]],
    export_options: Optional[Dict[str, Any]],
    conflict_strategy: str,
    content_hash: str,
    disabled: bool = False,
    existing_id: Optional[int] = None,
) -> int:
    """保存或更新批量任务模板（按 name 唯一）."""
    params_json = json.dumps(execution_params, ensure_ascii=False) if execution_params else None
    env_json = json.dumps(env_whitelist, ensure_ascii=False) if env_whitelist else None
    export_json = json.dumps(export_options, ensure_ascii=False) if export_options else None

    with get_conn(db_path) as conn:
        if existing_id is not None:
            conn.execute(
                """UPDATE batch_task_templates SET
                   description = ?, execution_params = ?, env_whitelist = ?,
                   export_options = ?, conflict_strategy = ?, content_hash = ?,
                   disabled = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (description, params_json, env_json, export_json,
                 conflict_strategy, content_hash, 1 if disabled else 0,
                 existing_id),
            )
            conn.commit()
            return existing_id

        row = conn.execute(
            "SELECT id FROM batch_task_templates WHERE name = ?", (name,)
        ).fetchone()
        if row:
            conn.execute(
                """UPDATE batch_task_templates SET
                   description = ?, execution_params = ?, env_whitelist = ?,
                   export_options = ?, conflict_strategy = ?, content_hash = ?,
                   disabled = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (description, params_json, env_json, export_json,
                 conflict_strategy, content_hash, 1 if disabled else 0,
                 row["id"]),
            )
            conn.commit()
            return row["id"]

        cursor = conn.execute(
            """INSERT INTO batch_task_templates
               (name, description, execution_params, env_whitelist,
                export_options, conflict_strategy, content_hash, disabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, description, params_json, env_json, export_json,
             conflict_strategy, content_hash, 1 if disabled else 0),
        )
        conn.commit()
        return cursor.lastrowid


def get_batch_template(db_path: str, name: str) -> Optional[Dict[str, Any]]:
    """按名称获取批量任务模板."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batch_task_templates WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_batch_template(row) if row else None


def get_batch_template_by_id(db_path: str, template_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 获取批量任务模板."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM batch_task_templates WHERE id = ?", (template_id,)
        ).fetchone()
        return _row_to_batch_template(row) if row else None


def list_batch_templates(db_path: str, include_disabled: bool = True) -> List[Dict[str, Any]]:
    """列出所有批量任务模板（按更新时间倒序）."""
    query = "SELECT * FROM batch_task_templates"
    params: List[Any] = []
    if not include_disabled:
        query += " WHERE disabled = 0"
    query += " ORDER BY updated_at DESC"
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_batch_template(r) for r in rows]


def delete_batch_template(db_path: str, name: str) -> bool:
    """删除批量任务模板."""
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM batch_task_templates WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0


def set_batch_template_disabled(db_path: str, name: str, disabled: bool) -> bool:
    """启用/禁用批量任务模板."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE batch_task_templates SET disabled = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (1 if disabled else 0, name),
        )
        conn.commit()
        return cursor.rowcount > 0


def rename_batch_template(db_path: str, old_name: str, new_name: str) -> bool:
    """重命名批量任务模板."""
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            "UPDATE batch_task_templates SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?",
            (new_name, old_name),
        )
        conn.commit()
        return cursor.rowcount > 0


# ============================================================================
# 批量任务模板历史 (batch_template_history) — 用于撤销
# ============================================================================

def append_batch_template_history(
    db_path: str,
    template_id: int,
    template_name: str,
    snapshot_before: Optional[Dict[str, Any]],
    action: str,
    operator: Optional[str] = None,
) -> int:
    """追加一条变更历史，用于 undo."""
    snapshot_json = json.dumps(snapshot_before, ensure_ascii=False) if snapshot_before else None
    with get_conn(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO batch_template_history
               (template_id, template_name, snapshot_before, action, operator)
               VALUES (?, ?, ?, ?, ?)""",
            (template_id, template_name, snapshot_json, action, operator),
        )
        conn.commit()
        return cursor.lastrowid


def get_last_batch_template_history(db_path: str, template_name: str) -> Optional[Dict[str, Any]]:
    """获取指定模板最近一条变更历史（用于撤销）."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM batch_template_history
               WHERE template_name = ? ORDER BY id DESC LIMIT 1""",
            (template_name,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("snapshot_before"):
            try:
                d["snapshot_before"] = json.loads(d["snapshot_before"])
            except (json.JSONDecodeError, TypeError):
                d["snapshot_before"] = None
        return d


def delete_batch_template_history(db_path: str, history_id: int) -> bool:
    """删除某条历史记录（撤销成功后清理）."""
    with get_conn(db_path) as conn:
        cursor = conn.execute("DELETE FROM batch_template_history WHERE id = ?", (history_id,))
        conn.commit()
        return cursor.rowcount > 0
