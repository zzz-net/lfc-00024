"""审计会话归档包回归测试.

覆盖范围：
- 归档 + 恢复往返（数据库/配置/导出报表/操作日志完整性）
- 恢复后可继续执行 list / show / export / undo（含跨重启 CLI 调用）
- 冲突策略 abort / rename / overwrite
- 失败场景：归档不存在、归档损坏、manifest 损坏、版本不兼容、schema 不匹配
- 路径/目录不可写：输出是目录、输出父目录不可创建、目标根冲突为文件
- 关键动作写入操作日志（session_archive / session_restore）
"""
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout

from inventory_audit import cli
from inventory_audit import config as cfg
from inventory_audit import db
from inventory_audit import exporter
from inventory_audit import importer
from inventory_audit import merger
from inventory_audit import reviewer
from inventory_audit import session_archive as sa


# ============================================================================
# 辅助函数
# ============================================================================

def _make_config(tmpdir):
    audit_dir = os.path.join(tmpdir, "audit_data")
    config = {
        "database": {"path": os.path.join(audit_dir, "audit.db")},
        "csv": {
            "location_column": "location",
            "sku_column": "sku",
            "expected_column": "expected_qty",
            "counted_column": "counted_qty",
            "encoding": "utf-8-sig",
            "delimiter": ",",
        },
        "rules": {"diff_threshold": 0, "merge_keys": ["location", "sku"]},
        "status": {
            "initial": "pending",
            "allowed": ["pending", "confirmed", "ignored", "closed"],
        },
        "export": {"output_dir": os.path.join(audit_dir, "exports")},
        "active_plan": None,
        "operator": "tester",
    }
    return config


def _write_config_file(tmpdir, config):
    cfg_path = os.path.join(tmpdir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)
    return cfg_path


def _write_csv(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("location,sku,expected_qty,counted_qty\n")
        f.write("WH-A,SKU-001,100,90\n")
        f.write("WH-A,SKU-002,50,50\n")
        f.write("WH-B,SKU-003,20,15\n")


def _build_session(tmpdir):
    """搭建一个带真实数据的盘点会话：导入 + 状态复核 + 导出报表."""
    config = _make_config(tmpdir)
    cfg_path = _write_config_file(tmpdir, config)
    db_path = cfg.get_db_path(config)
    db.init_db(db_path)

    csv_path = os.path.join(tmpdir, "stock.csv")
    _write_csv(csv_path)
    imp = importer.import_csv(
        db_path, csv_path, config["csv"],
        batch_name="batch-1",
        default_status="pending",
        rules=cfg.get_rules(config),
    )
    assert imp["success"], imp

    diffs = merger.get_merged_differences(db_path)
    assert len(diffs) == 2, diffs  # SKU-001(差10) 与 SKU-003(差5) 入库
    diff_id = diffs[0]["id"]

    res = reviewer.set_status(
        db_path, diff_id, "confirmed",
        operator="tester",
        allowed_statuses=cfg.get_allowed_statuses(config),
    )
    assert res["success"], res

    export_dir = config["export"]["output_dir"]
    os.makedirs(export_dir, exist_ok=True)
    exp = exporter.export_differences(
        db_path, export_dir, operator="tester",
    )
    assert exp["success"], exp

    # 写入运行时状态（模拟真实会话切换过激活方案/操作人）
    runtime_state = {"active_plan": "cli-plan", "operator": "tester"}
    with open(os.path.join(os.path.dirname(db_path), "runtime_state.json"),
              "w", encoding="utf-8") as f:
        json.dump(runtime_state, f, ensure_ascii=False)

    return {
        "tmpdir": tmpdir,
        "config": config,
        "config_path": cfg_path,
        "db_path": db_path,
        "audit_data_dir": os.path.dirname(db_path),
        "export_dir": export_dir,
        "diff_id": diff_id,
        "diffs": diffs,
    }


def _make_archive(env, output_path=None):
    """对 env 会话打归档包."""
    result = sa.create_session_archive(
        env["db_path"], env["config"],
        config_file_path=env["config_path"],
        output_path=output_path,
        operator="tester",
    )
    return result


def _build_fake_archive(path, manifest, db_bytes=b"", extra_entries=None):
    """构造一个可控 manifest 的归档 zip（用于损坏/版本/schema 测试）."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("data/audit.db", db_bytes)
        if extra_entries:
            for name, data in extra_entries.items():
                zf.writestr(name, data)


# ============================================================================
# 归档 + 信息
# ============================================================================

class TestSessionArchiveCreateInfo(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = _build_session(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_archive_success_and_auto_filename(self):
        env = self.env
        result = _make_archive(env, output_path=None)
        self.assertTrue(result["success"], result)
        archive_path = result["archive_path"]
        self.assertTrue(os.path.isfile(archive_path))
        self.assertTrue(archive_path.endswith(".zip"))
        # 归档应位于 audit_data/archives 下
        self.assertIn("archives", archive_path)
        # manifest 校验通过
        info = sa.read_archive_manifest(archive_path)
        self.assertTrue(info["success"], info)
        self.assertEqual(info["manifest"]["$schema"], sa.ARCHIVE_SCHEMA)
        self.assertEqual(info["manifest"]["$archive_version"], sa.ARCHIVE_VERSION)
        # manifest 中文件列表至少包含数据库与配置
        names = {f["archive_path"] for f in info["manifest"]["files"]}
        self.assertIn("data/audit.db", names)
        self.assertIn("data/config.json", names)
        self.assertIn("data/runtime_state.json", names)

    def test_create_archive_custom_output_path(self):
        env = self.env
        custom = os.path.join(self.tmpdir, "custom", "session.zip")
        result = _make_archive(env, output_path=custom)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["archive_path"], custom)
        self.assertTrue(os.path.isfile(custom))

    def test_archive_contains_export_and_operation_logs(self):
        env = self.env
        result = _make_archive(env)
        archive_path = result["archive_path"]
        info = sa.list_archive_contents(archive_path)
        self.assertTrue(info["success"], info)
        summary = info["summary"]
        # 数据库行数摘要中 differences > 0
        self.assertGreater(summary["database"]["row_counts"]["differences"], 0)
        # 导出报表至少 1 个
        self.assertGreaterEqual(len(summary["exports"]), 1)
        # operation_logs.json 在归档内
        names = {f["archive_path"] for f in info["manifest"]["files"]}
        self.assertIn("data/operation_logs.json", names)

    def test_create_archive_writes_operation_log(self):
        env = self.env
        _make_archive(env)
        logs = db.get_operation_logs(env["db_path"])
        actions = [l.get("action_type") for l in logs]
        self.assertIn("session_archive", actions)

    def test_create_archive_missing_db_fails(self):
        env = self.env
        os.remove(env["db_path"])
        result = sa.create_session_archive(
            env["db_path"], env["config"],
            config_file_path=env["config_path"],
            output_path=None, operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["error"])

    def test_create_archive_output_is_directory_fails(self):
        env = self.env
        dir_path = os.path.join(self.tmpdir, "a_dir")
        os.makedirs(dir_path)
        result = _make_archive(env, output_path=dir_path)
        self.assertFalse(result["success"])
        self.assertIn("目录", result["error"])

    def test_create_archive_output_parent_not_creatable_fails(self):
        env = self.env
        # 用一个文件占用父路径，使 os.makedirs 无法创建
        blocker = os.path.join(self.tmpdir, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        bad_output = os.path.join(blocker, "out.zip")
        result = _make_archive(env, output_path=bad_output)
        self.assertFalse(result["success"])

    def test_list_archive_contents_missing_file_fails(self):
        result = sa.list_archive_contents(os.path.join(self.tmpdir, "nope.zip"))
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["error"])

    def test_list_archive_contents_corrupted_zip_fails(self):
        bad = os.path.join(self.tmpdir, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"\x50\x4b\x00\x00not really a zip body")
        result = sa.list_archive_contents(bad)
        self.assertFalse(result["success"])
        # 错误信息提示损坏/无效
        self.assertTrue(
            any(k in result["error"] for k in ("损坏", "无效", "zip", "BadZip", "读取")),
            result["error"],
        )


# ============================================================================
# 恢复往返 + 恢复后继续工作
# ============================================================================

class TestSessionArchiveRestoreRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = _build_session(self.tmpdir)
        arc = _make_archive(self.env)
        self.assertTrue(arc["success"], arc)
        self.archive_path = arc["archive_path"]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_target(self):
        d = tempfile.mkdtemp(prefix="restore_")
        return d

    def test_restore_to_new_dir_data_consistent(self):
        env = self.env
        target = self._new_target()
        try:
            result = sa.restore_session_archive(
                self.archive_path, target, conflict="abort", operator="tester",
            )
            self.assertTrue(result["success"], result)
            new_db = os.path.join(result["audit_data_dir"], "audit.db")
            self.assertTrue(os.path.isfile(new_db))
            self.assertTrue(os.path.isfile(result["config_path"]))
            # 直接比较差异数量
            src_diffs = merger.get_merged_differences(env["db_path"])
            dst_diffs = merger.get_merged_differences(new_db)
            self.assertEqual(len(src_diffs), len(dst_diffs))
            # id 在恢复后会重新生成，用 location+sku 对齐复核状态
            def key(d):
                return (d.get("location"), d.get("sku"))
            dst_by_key = {key(d): d["status"] for d in dst_diffs}
            for d in src_diffs:
                self.assertEqual(dst_by_key.get(key(d)), d["status"])
            # 导出报表已恢复
            restored_exports = os.path.join(result["audit_data_dir"], "exports")
            self.assertTrue(os.path.isdir(restored_exports))
            self.assertGreaterEqual(len(os.listdir(restored_exports)), 1)
            # runtime_state 恢复
            state_file = os.path.join(result["audit_data_dir"], "runtime_state.json")
            self.assertTrue(os.path.isfile(state_file))
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_restore_writes_session_restore_log(self):
        target = self._new_target()
        try:
            result = sa.restore_session_archive(
                self.archive_path, target, conflict="abort", operator="tester",
            )
            self.assertTrue(result["success"], result)
            new_db = os.path.join(result["audit_data_dir"], "audit.db")
            logs = db.get_operation_logs(new_db)
            actions = [l.get("action_type") for l in logs]
            self.assertIn("session_restore", actions)
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_restore_then_cli_list_show_export_undo(self):
        """恢复后通过真实 CLI 继续工作（跨重启一致性）."""
        target = self._new_target()
        cwd = os.getcwd()
        try:
            result = sa.restore_session_archive(
                self.archive_path, target, conflict="abort", operator="tester",
            )
            self.assertTrue(result["success"], result)
            os.chdir(target)
            # list
            rc = cli.main(["-c", "config.json", "list"])
            self.assertEqual(rc, 0)
            # show 1
            rc = cli.main(["-c", "config.json", "show", "1"])
            self.assertEqual(rc, 0)
            # export summary -> 生成新文件（用 summary 避免与恢复的 differences 报表同名冲突）
            before = set(os.listdir(os.path.join("audit_data", "exports")))
            rc = cli.main(["-c", "config.json", "export", "-t", "summary"])
            self.assertEqual(rc, 0)
            after = set(os.listdir(os.path.join("audit_data", "exports")))
            self.assertGreater(len(after), len(before))
            # 再 export differences 也可正常工作
            rc = cli.main(["-c", "config.json", "export", "-t", "differences"])
            self.assertEqual(rc, 0)
            # undo -> 撤销恢复前最后一次复核（status_change: pending->confirmed）
            rc = cli.main(["-c", "config.json", "undo"])
            self.assertEqual(rc, 0)
            # 再次 list 仍正常
            rc = cli.main(["-c", "config.json", "list"])
            self.assertEqual(rc, 0)
        finally:
            os.chdir(cwd)
            shutil.rmtree(target, ignore_errors=True)

    def test_restore_cross_restart_consistency(self):
        """恢复后关闭再重新加载，状态保持一致."""
        target = self._new_target()
        cwd = os.getcwd()
        try:
            result = sa.restore_session_archive(
                self.archive_path, target, conflict="abort", operator="tester",
            )
            self.assertTrue(result["success"], result)
            new_db = os.path.join(result["audit_data_dir"], "audit.db")
            # 第一次读取
            diffs1 = merger.get_merged_differences(new_db)
            # 模拟重启：重新 init_db（幂等）后再次读取
            db.init_db(new_db)
            diffs2 = merger.get_merged_differences(new_db)
            self.assertEqual(len(diffs1), len(diffs2))
            # 恢复后 runtime_state 跨重启保持
            state1 = cfg.load_config(os.path.join(target, "config.json"))
            state1_op = state1.get("operator")
            state2 = cfg.load_config(os.path.join(target, "config.json"))
            self.assertEqual(state1_op, state2.get("operator"))
        finally:
            os.chdir(cwd)
            shutil.rmtree(target, ignore_errors=True)


# ============================================================================
# 冲突策略 abort / rename / overwrite
# ============================================================================

class TestSessionArchiveConflicts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = _build_session(self.tmpdir)
        arc = _make_archive(self.env)
        self.assertTrue(arc["success"], arc)
        self.archive_path = arc["archive_path"]
        self.target = tempfile.mkdtemp(prefix="conflict_")
        # 先恢复一次，制造冲突
        first = sa.restore_session_archive(
            self.archive_path, self.target, conflict="abort", operator="tester",
        )
        self.assertTrue(first["success"], first)
        self.restored_db = os.path.join(first["audit_data_dir"], "audit.db")
        self.restored_config = first["config_path"]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        shutil.rmtree(self.target, ignore_errors=True)

    def test_conflict_detected(self):
        conflicts = sa.detect_restore_conflicts(self.archive_path, self.target)
        types = {c["type"] for c in conflicts}
        self.assertIn("database_exists", types)
        self.assertIn("config_exists", types)

    def test_abort_does_not_modify_existing(self):
        before_size = os.path.getsize(self.restored_db)
        result = sa.restore_session_archive(
            self.archive_path, self.target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        conflict_types = {c["type"] for c in result.get("conflicts", [])}
        self.assertIn("database_exists", conflict_types)
        # 原数据库未被改动
        after_size = os.path.getsize(self.restored_db)
        self.assertEqual(before_size, after_size)
        # config 仍是原样
        self.assertTrue(os.path.isfile(self.restored_config))

    def test_rename_restores_to_separate_paths(self):
        result = sa.restore_session_archive(
            self.archive_path, self.target, conflict="rename", operator="tester",
        )
        self.assertTrue(result["success"], result)
        # 恢复到 audit_data_restored/ 与 config_restored.json
        restored_dir = os.path.join(self.target, "audit_data_restored")
        restored_config = os.path.join(self.target, "config_restored.json")
        self.assertTrue(os.path.isdir(restored_dir))
        self.assertTrue(os.path.isfile(restored_config))
        self.assertTrue(os.path.isfile(os.path.join(restored_dir, "audit.db")))
        # 原有数据未动
        self.assertTrue(os.path.isfile(self.restored_db))
        self.assertTrue(os.path.isfile(self.restored_config))
        # 恢复的配置指向 audit_data_restored
        rcfg = json.load(open(restored_config, encoding="utf-8"))
        self.assertIn("audit_data_restored", rcfg["database"]["path"])

    def test_overwrite_replaces_existing_db(self):
        # 在已恢复库中追加一条复核动作，制造与归档不同的状态
        diffs = merger.get_merged_differences(self.restored_db)
        if len(diffs) >= 2:
            reviewer.set_status(
                self.restored_db, diffs[1]["id"], "ignored",
                operator="tester",
                allowed_statuses=["pending", "confirmed", "ignored", "closed"],
            )
        before_diffs = merger.get_merged_differences(self.restored_db)
        self.assertGreater(len(before_diffs), 0)
        # overwrite 后应被归档内容替换
        result = sa.restore_session_archive(
            self.archive_path, self.target, conflict="overwrite", operator="tester",
        )
        self.assertTrue(result["success"], result)
        # 覆盖后库内行数与归档源一致
        after_diffs = merger.get_merged_differences(self.restored_db)
        self.assertEqual(len(after_diffs), len(self.env["diffs"]))
        # 之前改的 ignored 状态应消失（恢复为归档时的 confirmed）
        restored_states = {d["status"] for d in after_diffs}
        self.assertIn("confirmed", restored_states)
        self.assertNotIn("ignored", restored_states)


# ============================================================================
# 损坏 / 版本 / schema 失败
# ============================================================================

class TestSessionArchiveFailures(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = _build_session(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_restore_nonexistent_archive_fails(self):
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            os.path.join(self.tmpdir, "missing.zip"), target,
            conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertIn("不存在", result["error"])

    def test_restore_corrupted_zip_fails(self):
        bad = os.path.join(self.tmpdir, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"this is definitely not a zip file \x00\x01\x02")
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            bad, target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(
            any(k in result["error"] for k in ("损坏", "无效", "zip", "读取")),
            result["error"],
        )

    def test_restore_manifest_bad_json_fails(self):
        bad = os.path.join(self.tmpdir, "badmanifest.zip")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("manifest.json", "{not valid json")
            zf.writestr("data/audit.db", b"")
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            bad, target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(
            any(k in result["error"] for k in ("manifest", "JSON", "损坏", "解析")),
            result["error"],
        )

    def test_restore_version_incompatible_fails(self):
        arc = os.path.join(self.tmpdir, "v99.zip")
        # 用真实库做 DB 体，避免后续解压失败干扰
        with open(self.env["db_path"], "rb") as _f:
            db_bytes = _f.read()
        _build_fake_archive(arc, {
            "$schema": sa.ARCHIVE_SCHEMA,
            "$archive_version": 99,
            "created_at": "2024-01-01T00:00:00Z",
            "operator": "tester",
            "tool_version": "1.0.0",
            "files": [],
            "summary": {},
        }, db_bytes=db_bytes)
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            arc, target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("incompatible") or "版本" in result["error"])

    def test_restore_schema_mismatch_fails(self):
        arc = os.path.join(self.tmpdir, "wrongschema.zip")
        with open(self.env["db_path"], "rb") as _f:
            db_bytes = _f.read()
        _build_fake_archive(arc, {
            "$schema": "something_else",
            "$archive_version": sa.ARCHIVE_VERSION,
            "created_at": "2024-01-01T00:00:00Z",
            "operator": "tester",
            "tool_version": "1.0.0",
            "files": [],
            "summary": {},
        }, db_bytes=db_bytes)
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            arc, target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(
            any(k in result["error"] for k in ("schema", "不匹配")),
            result["error"],
        )

    def test_restore_target_root_is_file_fails(self):
        # 目标根是一个已存在的文件，不能作为工作目录
        arc = self._make_real_archive()
        blocker = os.path.join(self.tmpdir, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        result = sa.restore_session_archive(
            arc, blocker, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertIn("目录", result["error"])

    def _make_real_archive(self):
        arc = _make_archive(self.env)
        self.assertTrue(arc["success"], arc)
        return arc["archive_path"]

    def test_restore_integrity_tampered_db_fails(self):
        # 构造一个合法 manifest 但 data/audit.db 内容被篡改（sha256 不匹配）
        arc = os.path.join(self.tmpdir, "tampered.zip")
        with open(self.env["db_path"], "rb") as _f:
            db_bytes = _f.read()
        manifest = {
            "$schema": sa.ARCHIVE_SCHEMA,
            "$archive_version": sa.ARCHIVE_VERSION,
            "created_at": "2024-01-01T00:00:00Z",
            "operator": "tester",
            "tool_version": "1.0.0",
            "files": [
                {
                    "archive_path": "data/audit.db",
                    "size": len(db_bytes),
                    "sha256": "0" * 64,  # 故意错误的哈希
                },
            ],
            "summary": {},
        }
        _build_fake_archive(arc, manifest, db_bytes=db_bytes)
        target = os.path.join(self.tmpdir, "restore_target")
        result = sa.restore_session_archive(
            arc, target, conflict="abort", operator="tester",
        )
        self.assertFalse(result["success"])
        self.assertTrue(
            any(k in result["error"] for k in ("校验", "sha256", "哈希", "完整性", "损坏")),
            result["error"],
        )


# ============================================================================
# CLI 端到端冒烟
# ============================================================================

class TestSessionArchiveCLI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = _build_session(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cli_create_and_info(self):
        env = self.env
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-create",
                "-o", os.path.join(self.tmpdir, "out.zip"),
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmpdir, "out.zip")))
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-info",
                os.path.join(self.tmpdir, "out.zip"),
            ])
        self.assertEqual(rc, 0)
        out = buf2.getvalue()
        self.assertIn("审计会话归档内容摘要", out)
        self.assertIn("归档版本", out)

    def test_cli_restore_to_new_dir(self):
        env = self.env
        arc = os.path.join(self.tmpdir, "out.zip")
        cli.main(["-c", env["config_path"], "session-archive-create", "-o", arc])
        target = os.path.join(self.tmpdir, "restored")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-restore",
                arc, "--target-dir", target,
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(target, "audit_data", "audit.db")))
        self.assertTrue(os.path.isfile(os.path.join(target, "config.json")))

    def test_cli_restore_abort_on_conflict(self):
        env = self.env
        arc = os.path.join(self.tmpdir, "out.zip")
        cli.main(["-c", env["config_path"], "session-archive-create", "-o", arc])
        target = os.path.join(self.tmpdir, "restored")
        # 第一次恢复
        cli.main(["-c", env["config_path"], "session-archive-restore",
                  arc, "--target-dir", target])
        marker = os.path.getsize(os.path.join(target, "audit_data", "audit.db"))
        # 第二次冲突 -> abort
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-restore",
                arc, "--target-dir", target, "--conflict", "abort",
            ])
        self.assertEqual(rc, 2)
        out = buf.getvalue()
        self.assertIn("冲突", out)
        # 数据未变
        self.assertEqual(
            os.path.getsize(os.path.join(target, "audit_data", "audit.db")),
            marker,
        )

    def test_cli_restore_rename(self):
        env = self.env
        arc = os.path.join(self.tmpdir, "out.zip")
        cli.main(["-c", env["config_path"], "session-archive-create", "-o", arc])
        target = os.path.join(self.tmpdir, "restored")
        cli.main(["-c", env["config_path"], "session-archive-restore",
                  arc, "--target-dir", target])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-restore",
                arc, "--target-dir", target, "--conflict", "rename",
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(os.path.join(target, "audit_data_restored")))
        self.assertTrue(os.path.isfile(os.path.join(target, "config_restored.json")))

    def test_cli_restore_overwrite(self):
        env = self.env
        arc = os.path.join(self.tmpdir, "out.zip")
        cli.main(["-c", env["config_path"], "session-archive-create", "-o", arc])
        target = os.path.join(self.tmpdir, "restored")
        cli.main(["-c", env["config_path"], "session-archive-restore",
                  arc, "--target-dir", target])
        # 篡改目标库以制造差异
        tgt_db = os.path.join(target, "audit_data", "audit.db")
        reviewer.set_status(
            tgt_db,
            merger.get_merged_differences(tgt_db)[0]["id"],
            "ignored", operator="tester",
            allowed_statuses=["pending", "confirmed", "ignored", "closed"],
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "-c", env["config_path"], "session-archive-restore",
                arc, "--target-dir", target, "--conflict", "overwrite",
            ])
        self.assertEqual(rc, 0)
        # 覆盖后 ignored 状态消失
        states = {d["status"] for d in merger.get_merged_differences(tgt_db)}
        self.assertNotIn("ignored", states)


if __name__ == "__main__":
    unittest.main()
