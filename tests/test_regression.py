"""可复现用例与回归测试 - 覆盖规则配置生效、缺列导入失败、忽略后撤销再导出链路."""
import csv
import json
import os
import shutil
import tempfile
import unittest

from inventory_audit import config as cfg
from inventory_audit import db
from inventory_audit import exporter
from inventory_audit import importer
from inventory_audit import merger
from inventory_audit import reviewer


def _make_config(tmpdir, **overrides):
    """构造一份真实可用的配置（非占位）."""
    config = {
        "database": {"path": os.path.join(tmpdir, "audit.db")},
        "csv": {
            "location_column": "location",
            "sku_column": "sku",
            "expected_column": "expected_qty",
            "counted_column": "counted_qty",
            "encoding": "utf-8-sig",
            "delimiter": ",",
        },
        "rules": {
            "diff_threshold": 0,
            "merge_keys": ["location", "sku"],
        },
        "status": {
            "initial": "pending",
            "allowed": ["pending", "confirmed", "ignored", "closed"],
        },
        "export": {"output_dir": os.path.join(tmpdir, "exports")},
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(config.get(key), dict):
            config[key].update(val)
        else:
            config[key] = val
    return config


def _write_csv(path, header, rows):
    """写入 CSV 文件."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_test_")
        self.config = _make_config(self.tmpdir)
        self.db_path = cfg.get_db_path(self.config)
        cfg.ensure_dirs(self.config)
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _import(self, csv_path, **kwargs):
        return importer.import_csv(
            self.db_path, csv_path, self.config["csv"],
            default_status=self.config["status"]["initial"],
            rules=self.config.get("rules", {}),
            **kwargs,
        )


class TestConfigDrivenRules(_BaseTest):
    """Bug1: 规则配置读进来了却没有真正驱动阈值和合并窗口."""

    def test_status_allowed_from_config_drives_validation(self):
        """config.status.allowed 必须驱动状态校验，而非硬编码集合."""
        self.config["status"]["allowed"] = ["pending", "confirmed", "closed"]
        csv_path = os.path.join(self.tmpdir, "t.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU1", "100", "90"]])
        self._import(csv_path)
        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 1)

        result = reviewer.set_status(
            self.db_path, diffs[0]["id"], "ignored",
            allowed_statuses=self.config["status"]["allowed"],
        )
        self.assertFalse(result["success"])
        self.assertIn("ignored", result["error"])

        result_ok = reviewer.set_status(
            self.db_path, diffs[0]["id"], "confirmed",
            allowed_statuses=self.config["status"]["allowed"],
        )
        self.assertTrue(result_ok["success"])

    def test_diff_threshold_filters_insignificant_diffs(self):
        """rules.diff_threshold 必须过滤掉低于阈值的差异."""
        self.config["rules"]["diff_threshold"] = 5
        csv_path = os.path.join(self.tmpdir, "t.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU_SMALL", "100", "98"],
                    ["A-01", "SKU_BIG", "100", "80"]])
        result = self._import(csv_path)
        self.assertTrue(result["success"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["below_threshold_skipped"], 1)

        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["sku"], "SKU_BIG")

    def test_merge_keys_configurable(self):
        """rules.merge_keys 必须驱动合并分组，而非硬编码 location+sku."""
        self.config["rules"]["merge_keys"] = ["sku"]
        csv_path = os.path.join(self.tmpdir, "t.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU1", "100", "90"],
                    ["A-02", "SKU1", "50", "40"]])
        result = self._import(csv_path)
        self.assertTrue(result["success"])

        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["total_diff_qty"], -20.0)


class TestMissingCountedQty(_BaseTest):
    """Bug2: 缺少 counted_qty 必须明确报错并阻止入库，不能默默按 0."""

    def test_missing_counted_qty_column_blocks_import(self):
        """CSV 完全缺少 counted_qty 列头时，整批导入失败."""
        csv_path = os.path.join(self.tmpdir, "no_counted.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty"],
                   [["A-01", "SKU1", "100"]])
        result = self._import(csv_path)
        self.assertFalse(result["success"])
        self.assertIn("counted_qty", result["error"])
        self.assertEqual(db.list_differences(self.db_path), [])
        self.assertEqual(db.list_batches(self.db_path), [])

    def test_empty_counted_qty_value_rejected(self):
        """单行 counted_qty 为空时该行被拒绝，不按 0 处理."""
        csv_path = os.path.join(self.tmpdir, "empty_counted.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU_BAD", "100", ""],
                    ["A-01", "SKU_OK", "100", "90"]])
        result = self._import(csv_path)
        self.assertTrue(result["success"])
        self.assertEqual(result["imported"], 1)

        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["sku"], "SKU_OK")
        self.assertEqual(diffs[0]["total_diff_qty"], -10.0)

    def test_counted_qty_not_silently_zero(self):
        """缺少 counted_qty 不能产生 diff = -expected 的脏数据."""
        csv_path = os.path.join(self.tmpdir, "silent_zero.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU1", "100", ""]])
        result = self._import(csv_path)
        self.assertFalse(result["success"])
        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 0)


class TestUndoAfterIgnore(_BaseTest):
    """Bug3: 标记忽略后撤销，状态必须完整恢复成待处理."""

    def _setup_diff(self):
        csv_path = os.path.join(self.tmpdir, "t.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU1", "100", "90"]])
        self._import(csv_path)
        return db.list_differences(self.db_path)[0]["id"]

    def test_undo_after_ignore_restores_pending(self):
        """忽略→撤销 后状态必须恢复为 pending，而非仍为 ignored."""
        diff_id = self._setup_diff()
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "pending")

        reviewer.set_status(self.db_path, diff_id, "ignored",
                           allowed_statuses=self.config["status"]["allowed"])
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "ignored")

        result = reviewer.undo_last(self.db_path)
        self.assertTrue(result["success"])
        self.assertEqual(result["action_type"], "status_change")

        diff = db.get_difference(self.db_path, diff_id)
        self.assertEqual(diff["status"], "pending",
                         "撤销后状态必须是 pending，不能带着 ignored 脏状态")

    def test_undo_after_ignore_then_export_shows_pending(self):
        """忽略→撤销→导出，导出结果必须显示待处理."""
        diff_id = self._setup_diff()
        reviewer.set_status(self.db_path, diff_id, "ignored",
                            allowed_statuses=self.config["status"]["allowed"])
        reviewer.undo_last(self.db_path)

        export_dir = os.path.join(self.tmpdir, "exports")
        result = exporter.export_differences(
            self.db_path, export_dir, status=None, include_sources=False
        )
        self.assertTrue(result["success"])
        with open(result["file_path"], "r", encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("待处理", content)
        self.assertNotIn("忽略", content)

    def test_undo_after_ignore_then_summary_shows_pending(self):
        """忽略→撤销→汇总，汇总必须显示 pending 计数，ignored 计数为 0."""
        diff_id = self._setup_diff()
        reviewer.set_status(self.db_path, diff_id, "ignored",
                            allowed_statuses=self.config["status"]["allowed"])
        reviewer.undo_last(self.db_path)

        summary = merger.get_merge_summary(self.db_path)
        by_status = summary["by_status"]
        self.assertEqual(by_status.get("ignored", {}).get("count", 0), 0)
        self.assertGreater(by_status.get("pending", {}).get("count", 0), 0)

    def test_undo_remark_does_not_touch_status(self):
        """撤销备注变更不应影响状态（边界检查）."""
        diff_id = self._setup_diff()
        reviewer.set_status(self.db_path, diff_id, "ignored",
                           allowed_statuses=self.config["status"]["allowed"])
        reviewer.set_remark(self.db_path, diff_id, "reason")

        reviewer.undo_last(self.db_path)
        diff = db.get_difference(self.db_path, diff_id)
        self.assertEqual(diff["status"], "ignored")
        self.assertEqual(diff["remark"], "")

        reviewer.undo_last(self.db_path)
        diff = db.get_difference(self.db_path, diff_id)
        self.assertEqual(diff["status"], "pending")

    def test_undo_works_after_remerge(self):
        """remerge 后撤销仍能正确恢复状态（review_history ID 已重映射）."""
        diff_id = self._setup_diff()
        reviewer.set_status(self.db_path, diff_id, "ignored",
                           allowed_statuses=self.config["status"]["allowed"])

        merger.remerge_all(self.db_path, merge_keys=self.config["rules"]["merge_keys"])

        diffs = db.list_differences(self.db_path)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["status"], "ignored")

        result = reviewer.undo_last(self.db_path)
        self.assertTrue(result["success"])
        self.assertEqual(result["action_type"], "status_change")

        diffs = db.list_differences(self.db_path)
        self.assertEqual(diffs[0]["status"], "pending",
                         "remerge 后撤销必须恢复为 pending，不能带着 ignored 脏状态")


class TestSchemaMigration(unittest.TestCase):
    """旧数据库迁移：补充 merge_key 列，不破坏已有批次."""

    def test_migrate_old_db_adds_merge_key(self):
        """旧版数据库（无 merge_key 列）迁移后仍可正常导入和查询."""
        tmpdir = tempfile.mkdtemp(prefix="audit_migrate_")
        try:
            config = _make_config(tmpdir)
            db_path = cfg.get_db_path(config)
            cfg.ensure_dirs(config)

            old_schema = """
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                UNIQUE(file_hash)
            );
            CREATE TABLE source_lines (
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
            CREATE TABLE differences (
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
            CREATE TABLE diff_sources (
                difference_id INTEGER NOT NULL,
                source_line_id INTEGER NOT NULL,
                PRIMARY KEY (difference_id, source_line_id)
            );
            CREATE TABLE review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                difference_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT,
                old_remark TEXT,
                new_remark TEXT,
                operator TEXT DEFAULT 'cli',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.executescript(old_schema)
            conn.execute(
                "INSERT INTO differences (location, sku, total_diff_qty, status) "
                "VALUES ('A-01', 'SKU1', -5, 'confirmed')"
            )
            conn.commit()
            conn.close()

            db.init_db(db_path)

            with db.get_conn(db_path) as conn:
                cols = conn.execute(
                    "PRAGMA table_info(differences)"
                ).fetchall()
            col_names = {c["name"] for c in cols}
            self.assertIn("merge_key", col_names)

            diffs = db.list_differences(db_path)
            self.assertEqual(len(diffs), 1)
            self.assertEqual(diffs[0]["status"], "confirmed")
            self.assertEqual(diffs[0]["merge_key"], "A-01|SKU1")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
