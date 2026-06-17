"""复核方案模板与批量执行回归测试.

覆盖：模板重名/内容冲突、字段缺失、配置损坏、执行中断后重试（不重复产生日志/导出物）、
版本漂移识别、重启续用（DB 丢失后从 JSON 落盘恢复）、导出元数据与模板版本对齐。
"""
import csv
import json
import os
import shutil
import tempfile
import unittest

from inventory_audit import archive as archive_mod
from inventory_audit import batch as batch_mod
from inventory_audit import cli
from inventory_audit import config as cfg
from inventory_audit import db
from inventory_audit import importer
from inventory_audit import templates as templates_mod


def _make_config(tmpdir, **overrides):
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
        "operator": "cli",
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(config.get(key), dict):
            config[key].update(val)
        else:
            config[key] = val
    return config


def _write_config_file(tmpdir, config):
    cfg_path = os.path.join(tmpdir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)
    return cfg_path


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_tpl_")
        self.config = _make_config(self.tmpdir)
        self.db_path = cfg.get_db_path(self.config)
        cfg.ensure_dirs(self.config)
        db.init_db(self.db_path)
        cfg.save_runtime_state(self.config)
        self.cfg_file = _write_config_file(self.tmpdir, self.config)
        self.output_dir = self.config["export"]["output_dir"]

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_cli(self, *argv):
        return cli.main(["-c", self.cfg_file] + list(argv))

    def _import(self, csv_path, **kwargs):
        return importer.import_csv(
            self.db_path, csv_path, self.config["csv"],
            default_status=self.config["status"]["initial"],
            rules=self.config.get("rules", {}),
            **kwargs,
        )

    def _make_batch(self, suffix="1"):
        csv_path = os.path.join(self.tmpdir, f"batch{suffix}.csv")
        _write_csv(
            csv_path,
            ["location", "sku", "expected_qty", "counted_qty"],
            [
                ["A-01", "SKU_A", "100", "90"],
                ["B-02", f"SKU_B_{suffix}", "50", "45"],
            ],
        )
        return csv_path

    def _save_template(self, name="T1", **kwargs):
        params = {
            "filters": {"status": None, "location": None, "sku": None},
            "export_fields": ["id", "location", "sku", "total_diff_qty", "status"],
            "remark_template": None,
            "steps": [{"action": "list"}, {"action": "export", "type": "differences"}],
            "description": None,
            "force": False,
        }
        params.update(kwargs)
        return templates_mod.save_template(self.db_path, self.config, name, **params)


class TestTemplateSaveCollision(_BaseTest):
    """模板重名与内容冲突：无变更 / 拒绝静默覆盖 / --force bump 版本."""

    def test_same_content_is_unchanged(self):
        r1 = self._save_template("T1")
        self.assertTrue(r1["success"])
        self.assertEqual(r1["action"], "created")
        self.assertEqual(r1["version"], 1)

        r2 = self._save_template("T1")
        self.assertTrue(r2["success"])
        self.assertEqual(r2["action"], "unchanged")
        self.assertEqual(r2["version"], 1)

    def test_content_change_without_force_is_rejected(self):
        self._save_template("T1")
        r = self._save_template(
            "T1", export_fields=["id", "location", "sku", "status", "remark"],
        )
        self.assertFalse(r["success"])
        self.assertTrue(r.get("conflict"))
        self.assertEqual(r["existing_version"], 1)
        self.assertIn("拒绝静默覆盖", r["message"])
        # 模板版本未变
        t = templates_mod.get_template(self.db_path, self.config, "T1")
        self.assertEqual(t["version"], 1)

    def test_content_change_with_force_bumps_version(self):
        self._save_template("T1")
        r = self._save_template(
            "T1",
            export_fields=["id", "location", "sku", "status", "remark"],
            force=True,
        )
        self.assertTrue(r["success"])
        self.assertEqual(r["action"], "overwritten")
        self.assertEqual(r["version"], 2)
        self.assertEqual(r["previous_version"], 1)
        # 再次保存相同内容应识别为 unchanged（基于 v2）
        r2 = self._save_template(
            "T1",
            export_fields=["id", "location", "sku", "status", "remark"],
        )
        self.assertEqual(r2["action"], "unchanged")
        self.assertEqual(r2["version"], 2)

    def test_duplicate_name_independent_templates(self):
        """不同名称各自独立创建."""
        self._save_template("T1")
        self._save_template("T2")
        names = [t["name"] for t in templates_mod.list_templates(self.db_path)]
        self.assertIn("T1", names)
        self.assertIn("T2", names)


class TestTemplateValidation(_BaseTest):
    """字段缺失与配置损坏：validate_template 覆盖各类非法结构."""

    def test_non_dict_is_corrupt(self):
        ok, err = templates_mod.validate_template([])
        self.assertFalse(ok)
        self.assertIn("根结构", err)

    def test_missing_name(self):
        ok, err = templates_mod.validate_template({"steps": []})
        self.assertFalse(ok)
        self.assertIn("name", err)

    def test_name_with_path_separator(self):
        ok, err = templates_mod.validate_template({"name": "a/b"})
        self.assertFalse(ok)
        self.assertIn("路径分隔符", err)

    def test_invalid_step_action(self):
        ok, err = templates_mod.validate_template({
            "name": "T", "steps": [{"action": "bogus"}],
        })
        self.assertFalse(ok)
        self.assertIn("action", err)

    def test_invalid_export_type(self):
        ok, err = templates_mod.validate_template({
            "name": "T", "steps": [{"action": "export", "type": "bogus"}],
        })
        self.assertFalse(ok)
        self.assertIn("export.type", err)

    def test_invalid_filters_key(self):
        ok, err = templates_mod.validate_template({
            "name": "T", "filters": {"warehouse": "W1"},
        })
        self.assertFalse(ok)
        self.assertIn("未知键", err)

    def test_valid_minimal_template(self):
        ok, err = templates_mod.validate_template({"name": "T"})
        self.assertTrue(ok)


class TestTemplateImportCorruption(_BaseTest):
    """导入配置损坏 / 字段缺失 / 重名冲突."""

    def _write_json(self, name, data):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return path

    def test_nonexistent_file(self):
        r = templates_mod.import_template(
            self.db_path, self.config, os.path.join(self.tmpdir, "nope.json"),
        )
        self.assertFalse(r["success"])
        self.assertIn("不存在", r["error"])

    def test_malformed_json(self):
        path = os.path.join(self.tmpdir, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json ")
        r = templates_mod.import_template(self.db_path, self.config, path)
        self.assertFalse(r["success"])
        self.assertIn("损坏", r["error"])

    def test_missing_name_field(self):
        path = self._write_json("noname.json", {"steps": [{"action": "list"}]})
        r = templates_mod.import_template(self.db_path, self.config, path)
        self.assertFalse(r["success"])
        self.assertIn("name", r["error"])

    def test_import_creates_then_conflict(self):
        path = self._write_json("tpl.json", {
            "name": "IMP", "version": 1,
            "filters": {"status": "pending"},
            "export_fields": ["id", "status"],
            "steps": [{"action": "list"}],
        })
        r1 = templates_mod.import_template(self.db_path, self.config, path)
        self.assertTrue(r1["success"])
        self.assertEqual(r1["action"], "created")

        # 修改内容后再次导入同名 → 冲突
        path2 = self._write_json("tpl2.json", {
            "name": "IMP", "version": 1,
            "filters": {"status": "confirmed"},
            "export_fields": ["id", "status"],
            "steps": [{"action": "list"}],
        })
        r2 = templates_mod.import_template(self.db_path, self.config, path2)
        self.assertFalse(r2["success"])
        self.assertTrue(r2.get("conflict"))

        # force 覆盖
        r3 = templates_mod.import_template(
            self.db_path, self.config, path2, force=True,
        )
        self.assertTrue(r3["success"])
        self.assertEqual(r3["action"], "overwritten")
        self.assertEqual(r3["version"], 2)

    def test_export_then_import_roundtrip(self):
        self._save_template("RT", description="roundtrip")
        out = os.path.join(self.tmpdir, "exported.json")
        r = templates_mod.export_template(self.db_path, self.config, "RT", out)
        self.assertTrue(r["success"])
        # 删除后从导出文件重新导入
        templates_mod.delete_template(self.db_path, self.config, "RT")
        self.assertIsNone(templates_mod.get_template(self.db_path, self.config, "RT"))
        r2 = templates_mod.import_template(self.db_path, self.config, out)
        self.assertTrue(r2["success"])
        t = templates_mod.get_template(self.db_path, self.config, "RT")
        self.assertEqual(t["version"], 1)
        self.assertEqual(t["description"], "roundtrip")


class TestBatchInterruptionRetry(_BaseTest):
    """执行中断后重试：已完成步骤保留，重试不重复产生日志/导出物."""

    def test_resume_skips_done_and_does_not_duplicate(self):
        # 模板步骤：summary（空库也能成功）→ differences（空库失败中断）
        self._save_template(
            "BT", steps=[
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ],
        )
        template = templates_mod.get_template(self.db_path, self.config, "BT")

        allowed = cfg.get_allowed_statuses(self.config)
        out1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        # 第一阶段：summary 完成，differences 失败中断
        self.assertFalse(out1["success"])
        self.assertEqual(out1["status"], "failed")
        self.assertEqual(out1["steps_done"], 1)
        self.assertEqual(out1["steps_failed"], 1)
        eid = out1["execution_id"]

        summary_files_1 = [f for f in os.listdir(self.output_dir)
                           if f.startswith("summary")]
        diff_files_1 = [f for f in os.listdir(self.output_dir)
                        if f.startswith("audit_report")]
        self.assertEqual(len(summary_files_1), 1)
        self.assertEqual(len(diff_files_1), 0)

        export_logs_1 = db.get_operation_logs(self.db_path, action_type="export")
        self.assertEqual(len(export_logs_1), 1)  # 仅 summary

        # 导入数据后续跑同一 execution_id
        self._import(self._make_batch())

        out2 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, execution_id=eid,
        )
        self.assertTrue(out2["success"])
        self.assertEqual(out2["status"], "completed")
        self.assertEqual(out2["steps_done"], 2)
        self.assertEqual(out2["steps_failed"], 0)

        # summary 文件不重复（仍 1 个），differences 新增 1 个
        summary_files_2 = [f for f in os.listdir(self.output_dir)
                           if f.startswith("summary")]
        diff_files_2 = [f for f in os.listdir(self.output_dir)
                        if f.startswith("audit_report")]
        self.assertEqual(len(summary_files_2), 1, "summary 不应重复产生")
        self.assertEqual(len(diff_files_2), 1)

        # 导出日志总共 2 条（summary 1 + differences 1），不重复
        export_logs_2 = db.get_operation_logs(self.db_path, action_type="export")
        self.assertEqual(len(export_logs_2), 2, "导出日志不应重复产生")

        # 续跑时第一步应标记为 skipped_done
        step0 = out2["steps"][0]
        self.assertEqual(step0["status"], "skipped_done")

    def test_fresh_execution_after_completed(self):
        """已完成的执行不会被重复执行；新建执行独立."""
        self._import(self._make_batch())
        self._save_template("BT2", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "BT2")
        allowed = cfg.get_allowed_statuses(self.config)

        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        self.assertTrue(r1["success"])
        eid = r1["execution_id"]

        # 再次以同一 execution_id 续跑 → 全部 skipped_done，无新增文件
        r2 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, execution_id=eid,
        )
        self.assertTrue(r2["success"])
        self.assertEqual(r2["steps_done"], 1)
        self.assertEqual(r2["steps"][0]["status"], "skipped_done")
        files = [f for f in os.listdir(self.output_dir)
                 if f.startswith("summary")]
        self.assertEqual(len(files), 1)


class TestVersionDrift(_BaseTest):
    """模板更新后已有执行记录不被静默改写，能识别版本差异."""

    def test_drift_detected_after_force_update(self):
        self._import(self._make_batch())
        self._save_template("VD", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "VD")
        allowed = cfg.get_allowed_statuses(self.config)

        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        self.assertTrue(r1["success"])
        eid = r1["execution_id"]

        # 强制更新模板 → v2
        self._save_template(
            "VD",
            steps=[{"action": "export", "type": "differences"}],
            force=True,
        )
        updated = templates_mod.get_template(self.db_path, self.config, "VD")
        self.assertEqual(updated["version"], 2)

        execution = db.get_execution(self.db_path, eid)
        # 执行记录仍冻结旧版本
        self.assertEqual(execution["template_version"], 1)
        self.assertIsNotNone(execution["template_snapshot"])

        drift = templates_mod.check_version_drift(
            self.db_path, self.config, execution,
        )
        self.assertIsNotNone(drift)
        self.assertTrue(drift["drift"])
        self.assertEqual(drift["execution_version"], 1)
        self.assertEqual(drift["current_version"], 2)

        # 续跑使用冻结快照（v1 的 summary 步骤），且报告版本漂移
        r2 = batch_mod.run_template(
            self.db_path, self.config, updated, self.output_dir,
            allowed_statuses=allowed, execution_id=eid,
        )
        self.assertIsNotNone(r2["version_drift"])
        # 快照步骤是 summary，应被跳过（已完成）
        self.assertEqual(r2["steps"][0]["status"], "skipped_done")

    def test_deleted_template_drift(self):
        self._import(self._make_batch())
        self._save_template("DEL", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "DEL")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        eid = r1["execution_id"]

        templates_mod.delete_template(self.db_path, self.config, "DEL")
        execution = db.get_execution(self.db_path, eid)
        drift = templates_mod.check_version_drift(
            self.db_path, self.config, execution,
        )
        self.assertIsNotNone(drift)
        self.assertEqual(drift["reason"], "template_missing")


class TestRestartContinuity(_BaseTest):
    """重启 CLI 后从 JSON 落盘文件恢复模板，继续使用同一套模板."""

    def test_template_recovers_from_json_after_db_loss(self):
        self._save_template("RC", description="restart continuity")
        template = templates_mod.get_template(self.db_path, self.config, "RC")
        tid = template["id"]

        # 模拟重启：删除数据库文件（JSON 落盘仍在）
        os.remove(self.db_path)
        db.init_db(self.db_path)  # 全新空库

        # 从 JSON 落盘恢复
        recovered = templates_mod.get_template(self.db_path, self.config, "RC")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["name"], "RC")
        self.assertEqual(recovered["description"], "restart continuity")
        self.assertEqual(recovered["version"], 1)

        # list_templates 也能看到
        names = [t["name"] for t in templates_mod.list_templates(self.db_path)]
        self.assertIn("RC", names)

        # 恢复后可继续批量执行
        self._import(self._make_batch())
        allowed = cfg.get_allowed_statuses(self.config)
        r = batch_mod.run_template(
            self.db_path, self.config, recovered, self.output_dir,
            allowed_statuses=allowed,
        )
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "completed")


class TestExportMetadataAlignment(_BaseTest):
    """导出结果、模板版本与执行日志三方对齐."""

    def test_export_carries_template_name_and_version(self):
        self._import(self._make_batch())
        self._save_template(
            "AL", export_fields=["id", "location", "sku", "status"],
            steps=[{"action": "export", "type": "differences"}],
        )
        template = templates_mod.get_template(self.db_path, self.config, "AL")
        allowed = cfg.get_allowed_statuses(self.config)

        r = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        self.assertTrue(r["success"])
        export_step = r["steps"][0]
        self.assertEqual(export_step["result"]["template_name"], "AL")
        self.assertEqual(export_step["result"]["template_version"], 1)

        # 文件名含模板标识 _tpl{id}v1
        file_path = export_step["result"]["file_path"]
        self.assertIn(f"_tpl{template['id']}v1", os.path.basename(file_path))

        # CSV 首行元数据含模板与版本
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            meta = next(reader)
        meta_text = ",".join(meta)
        self.assertIn("AL", meta_text)
        self.assertIn("v1", meta_text)

        # 操作日志携带模板字段
        logs = db.get_operation_logs(self.db_path, action_type="export")
        self.assertEqual(len(logs), 1)
        action_data = logs[0]["action_data"]
        self.assertEqual(action_data.get("template_name"), "AL")
        self.assertEqual(action_data.get("template_version"), 1)
        self.assertEqual(action_data.get("template_id"), template["id"])

        # 执行记录版本与模板版本一致
        eid = r["execution_id"]
        execution = db.get_execution(self.db_path, eid)
        self.assertEqual(execution["template_version"], 1)
        snap = execution["template_snapshot"]
        self.assertEqual(snap["name"], "AL")
        self.assertEqual(snap["version"], 1)


class TestCLIChain(_BaseTest):
    """CLI 全链路：新建 → 导入 → 批量执行 → 续跑，覆盖实际命令接线."""

    def test_cli_full_chain(self):
        # 新建模板
        rc = self._run_cli(
            "template-save", "CLI_T",
            "-f", "id,location,sku,status",
            "--steps", "list,export:summary",
            "-d", "cli chain",
        )
        self.assertEqual(rc, 0)

        # 导出模板到文件
        exp = os.path.join(self.tmpdir, "cli_tpl.json")
        rc = self._run_cli("template-export", "CLI_T", exp)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(exp))

        # 删除后从文件导入
        self._run_cli("template-delete", "CLI_T")
        rc = self._run_cli("template-import", exp)
        self.assertEqual(rc, 0)

        # 批量执行（无数据：list 通过，summary 通过）
        self._import(self._make_batch())
        rc = self._run_cli("template-run", "CLI_T")
        self.assertEqual(rc, 0)

        # template-list / template-show 不报错
        self.assertEqual(self._run_cli("template-list"), 0)
        self.assertEqual(self._run_cli("template-show", "CLI_T"), 0)

    def test_cli_conflict_exit_code(self):
        self._run_cli("template-save", "CLI_C", "--steps", "list")
        rc = self._run_cli("template-save", "CLI_C", "--steps", "export")
        self.assertEqual(rc, 1)
        rc = self._run_cli("template-save", "CLI_C", "--steps", "export", "--force")
        self.assertEqual(rc, 0)

    def test_cli_resume_flag(self):
        # 中断后用 --resume 续跑
        self._run_cli(
            "template-save", "CLI_R",
            "--steps", "export:summary,export:differences",
        )
        rc = self._run_cli("template-run", "CLI_R")
        self.assertEqual(rc, 2)  # 中断返回 2
        self._import(self._make_batch())
        rc = self._run_cli("template-run", "CLI_R", "--resume")
        self.assertEqual(rc, 0)


def _write_json_with_encoding(path, data, encoding):
    """把 JSON 以指定编码写入文件（utf-8-sig 会带 BOM，utf-8 不带）."""
    with open(path, "w", encoding=encoding, newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class TestBOMCompatibility(_BaseTest):
    """UTF-8 BOM 兼容回归：模板导入 / steps 文件 / 落盘恢复均不被带 BOM 的文件卡死.

    Windows 编辑器（Notepad、某些 VS Code 配置、Excel 另存为）常输出带 BOM 的 UTF-8。
    Python 默认 ``encoding="utf-8"`` 不会自动跳过 BOM，会把 ``\\ufeff`` 留在 JSON 字符串
    头部导致解析失败。修复点全部集中在导入相关模块的文件读取处。
    """

    def test_template_import_with_bom(self):
        """template-import 读取带 BOM 的 JSON，应与普通 UTF-8 一致成功."""
        tpl = {
            "name": "BOM_TPL",
            "version": 1,
            "description": "带 BOM 的模板",
            "filters": {"status": "pending"},
            "export_fields": ["id", "location", "sku", "status"],
            "steps": [
                {"action": "list"},
                {"action": "export", "type": "summary"},
            ],
        }
        bom_path = os.path.join(self.tmpdir, "tpl_bom.json")
        plain_path = os.path.join(self.tmpdir, "tpl_plain.json")
        _write_json_with_encoding(bom_path, tpl, "utf-8-sig")
        _write_json_with_encoding(plain_path, tpl, "utf-8")

        # 确认文件确实带 BOM
        with open(bom_path, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf", "测试前提：文件应带 UTF-8 BOM")

        # 带 BOM 导入成功
        r1 = templates_mod.import_template(self.db_path, self.config, bom_path)
        self.assertTrue(r1["success"], f"BOM 导入失败：{r1.get('error')}")
        self.assertEqual(r1["action"], "created")

        # 普通 UTF-8 再次重名导入（无变更）
        r2 = templates_mod.import_template(self.db_path, self.config, plain_path)
        self.assertTrue(r2["success"])
        self.assertEqual(r2["action"], "unchanged")

        # 读回来确认内容正确
        t = templates_mod.get_template(self.db_path, self.config, "BOM_TPL")
        self.assertIsNotNone(t)
        self.assertEqual(t["description"], "带 BOM 的模板")
        self.assertEqual(t["filters"]["status"], "pending")
        self.assertEqual(len(t["steps"]), 2)

    def test_steps_file_with_bom_via_cli(self):
        """template-save --steps-file 读取带 BOM 的 JSON，保存成功."""
        steps = [
            {"action": "list"},
            {"action": "export", "type": "differences"},
            {"action": "export", "type": "summary"},
        ]
        bom_steps = os.path.join(self.tmpdir, "steps_bom.json")
        _write_json_with_encoding(bom_steps, steps, "utf-8-sig")

        with open(bom_steps, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf")

        rc = self._run_cli(
            "template-save", "BOM_STEPS",
            "--steps-file", bom_steps,
            "-d", "带 BOM 的 steps",
        )
        self.assertEqual(rc, 0, "--steps-file 带 BOM 应解析成功并保存")

        t = templates_mod.get_template(self.db_path, self.config, "BOM_STEPS")
        self.assertIsNotNone(t)
        self.assertEqual(len(t["steps"]), 3)
        self.assertEqual(t["steps"][0]["action"], "list")
        self.assertEqual(t["steps"][1]["type"], "differences")
        self.assertEqual(t["steps"][2]["type"], "summary")

    def test_persisted_bom_template_recovers(self):
        """落盘的 templates/<name>.json 即使被外部编辑成带 BOM，重启后仍能恢复.

        （虽然我们自己写入用 utf-8 不带 BOM，但用户可能直接改文件）
        """
        # 先创建一个普通模板，触发落盘
        self._save_template("RC_BOM", description="会被改成带 BOM")
        tpl_file = os.path.join(
            os.path.dirname(self.db_path), "templates", "RC_BOM.json",
        )
        self.assertTrue(os.path.exists(tpl_file))

        # 用带 BOM 覆盖写（模拟用户用 Windows 记事本保存）
        with open(tpl_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        _write_json_with_encoding(tpl_file, data, "utf-8-sig")

        with open(tpl_file, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf")

        # 删除 DB 后恢复（走 get_template 的落盘回写路径）
        os.remove(self.db_path)
        db.init_db(self.db_path)

        recovered = templates_mod.get_template(self.db_path, self.config, "RC_BOM")
        self.assertIsNotNone(recovered, "带 BOM 的落盘文件应能恢复")
        self.assertEqual(recovered["name"], "RC_BOM")
        self.assertEqual(recovered["description"], "会被改成带 BOM")

    def test_plain_utf8_no_regression(self):
        """普通无 BOM 的 UTF-8 JSON 不应被破坏（仍正常导入）."""
        tpl = {
            "name": "NO_BOM",
            "steps": [{"action": "list"}, {"action": "export", "type": "summary"}],
        }
        path = os.path.join(self.tmpdir, "nobom.json")
        _write_json_with_encoding(path, tpl, "utf-8")
        # 确认无 BOM
        with open(path, "rb") as f:
            head = f.read(3)
        self.assertNotEqual(head, b"\xef\xbb\xbf")

        r = templates_mod.import_template(self.db_path, self.config, path)
        self.assertTrue(r["success"], f"普通 UTF-8 导入失败：{r.get('error')}")
        self.assertEqual(r["action"], "created")

    def test_cli_full_chain_with_bom_templates(self):
        """CLI 实际链路：从带 BOM 文件新建模板 → 批量执行 → 重启后恢复再执行."""
        # 1. 用带 BOM 的模板文件通过 CLI 导入
        tpl = {
            "name": "CHAIN_BOM",
            "version": 1,
            "description": "全链路 BOM 测试模板",
            "export_fields": ["id", "location", "sku", "status"],
            "steps": [
                {"action": "list"},
                {"action": "export", "type": "summary"},
            ],
        }
        bom_file = os.path.join(self.tmpdir, "chain_bom.json")
        _write_json_with_encoding(bom_file, tpl, "utf-8-sig")
        rc = self._run_cli("template-import", bom_file)
        self.assertEqual(rc, 0, "带 BOM 模板 CLI 导入应成功")

        # 2. 按模板批量执行
        self._import(self._make_batch("chain"))
        rc = self._run_cli("template-run", "CHAIN_BOM")
        self.assertEqual(rc, 0, "按导入模板批量执行应成功")

        # 3. 导出 + 再次导入（模拟跨环境分享）验证内容完整
        export_path = os.path.join(self.tmpdir, "exported.json")
        rc = self._run_cli("template-export", "CHAIN_BOM", export_path)
        self.assertEqual(rc, 0)

        # 删除后从导出文件再导入，用 CLI
        self._run_cli("template-delete", "CHAIN_BOM")
        rc = self._run_cli("template-import", export_path)
        self.assertEqual(rc, 0, "导出→删除→导入还原应成功")
        t = templates_mod.get_template(self.db_path, self.config, "CHAIN_BOM")
        self.assertIsNotNone(t)
        self.assertEqual(t["description"], "全链路 BOM 测试模板")
        self.assertEqual(len(t["steps"]), 2)


class TestExecutionArchiveExport(_BaseTest):
    """执行归档导出：template-run 完成/中断后自动导出，内容完整."""

    def test_auto_archive_on_completed_execution(self):
        """执行成功后自动生成归档文件."""
        self._import(self._make_batch())
        self._save_template(
            "ARC_CMP",
            steps=[
                {"action": "list"},
                {"action": "export", "type": "summary"},
            ],
        )
        template = templates_mod.get_template(self.db_path, self.config, "ARC_CMP")
        allowed = cfg.get_allowed_statuses(self.config)
        result = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, operator="tester_alice",
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result.get("archive_path"), "执行成功后应自动导出归档")
        self.assertTrue(os.path.exists(result["archive_path"]))

    def test_auto_archive_on_interrupted_execution(self):
        """执行中断后也自动生成归档文件."""
        self._save_template(
            "ARC_INT",
            steps=[
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ],
        )
        template = templates_mod.get_template(self.db_path, self.config, "ARC_INT")
        allowed = cfg.get_allowed_statuses(self.config)
        result = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, operator="tester_bob",
        )
        self.assertFalse(result["success"])
        self.assertIsNotNone(result.get("archive_path"), "中断后也应导出归档")
        self.assertTrue(os.path.exists(result["archive_path"]))

    def test_manifest_contains_required_fields(self):
        """归档清单包含：模板快照、步骤结果、operator、激活方案、导出文件、配置摘要."""
        cfg.set_active_plan(self.config, "test_plan_A")
        cfg.set_operator(self.config, "op_chen")
        cfg.save_runtime_state(self.config)
        self._import(self._make_batch())
        self._save_template(
            "ARC_FLD",
            steps=[
                {"action": "list"},
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ],
            export_fields=["id", "location", "sku", "status"],
        )
        template = templates_mod.get_template(self.db_path, self.config, "ARC_FLD")
        allowed = cfg.get_allowed_statuses(self.config)
        result = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        self.assertTrue(result["success"])

        with open(result["archive_path"], "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest["$schema"], "inventory_audit_execution_manifest")
        self.assertEqual(manifest["$manifest_version"], 1)

        snap = manifest["template_snapshot"]
        self.assertEqual(snap["name"], "ARC_FLD")
        self.assertEqual(snap["version"], 1)
        self.assertIn("filters", snap)
        self.assertIn("steps", snap)
        self.assertIn("content_hash", snap)
        self.assertEqual(snap["export_fields"], ["id", "location", "sku", "status"])

        meta = manifest["execution_meta"]
        self.assertEqual(meta["template_name"], "ARC_FLD")
        self.assertEqual(meta["template_version"], 1)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["operator"], "op_chen")
        self.assertEqual(meta["active_plan"], "test_plan_A")
        self.assertEqual(meta["steps_done"], 3)
        self.assertEqual(meta["steps_failed"], 0)

        self.assertEqual(manifest["operator"], "op_chen")
        self.assertEqual(manifest["active_plan"], "test_plan_A")

        self.assertEqual(len(manifest["steps"]), 3)
        self.assertEqual(manifest["steps"][0]["status"], "done")
        self.assertIsNotNone(manifest["steps"][0].get("result"))

        export_files = manifest["export_files"]
        self.assertGreaterEqual(len(export_files), 2)
        for ef in export_files:
            self.assertIn("file_path", ef)
            self.assertIn("template_name", ef)
            self.assertIn("template_version", ef)
            self.assertTrue(ef["file_exists"])

        cfg_sum = manifest["config_summary"]
        self.assertIn("csv_columns", cfg_sum)
        self.assertIn("status_allowed", cfg_sum)
        self.assertIn("merge_keys", cfg_sum)
        self.assertIn("export_output_dir", cfg_sum)

        self.assertGreaterEqual(len(manifest["operation_logs"]), 2)

    def test_explicit_export_execution_via_function(self):
        """显式调用 export_execution_manifest 导出指定 execution."""
        self._import(self._make_batch())
        self._save_template("ARC_EXP", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "ARC_EXP")
        allowed = cfg.get_allowed_statuses(self.config)
        r = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, auto_archive=False,
        )
        eid = r["execution_id"]

        out_path = os.path.join(self.tmpdir, "my_manifest.json")
        result = archive_mod.export_execution_manifest(
            self.db_path, self.config, eid, output_path=out_path,
        )
        self.assertTrue(result["success"])
        self.assertEqual(os.path.abspath(out_path), result["file_path"])
        self.assertTrue(os.path.exists(out_path))

        bad_result = archive_mod.export_execution_manifest(
            self.db_path, self.config, 99999,
        )
        self.assertFalse(bad_result["success"])


class TestExecutionArchiveRestore(_BaseTest):
    """从归档清单恢复执行历史：DB 丢失后恢复，template-show/--resume 正常."""

    def _setup_and_archive(self, template_name="ARC_RES", steps=None, operator="resume_op"):
        if steps is None:
            steps = [
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ]
        self._import(self._make_batch())
        cfg.set_operator(self.config, operator)
        cfg.save_runtime_state(self.config)
        self._save_template(template_name, steps=steps, description="归档恢复测试")
        template = templates_mod.get_template(self.db_path, self.config, template_name)
        allowed = cfg.get_allowed_statuses(self.config)
        result = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = result["archive_path"]
        self.assertIsNotNone(manifest_path)
        return result, manifest_path

    def test_restore_to_fresh_db(self):
        """删除 DB 后，从归档清单恢复模板和执行记录."""
        run_result, manifest_path = self._setup_and_archive()
        eid_orig = run_result["execution_id"]

        templates_dir = os.path.join(os.path.dirname(self.db_path), "templates")
        os.remove(self.db_path)
        if os.path.isdir(templates_dir):
            shutil.rmtree(templates_dir)
        db.init_db(self.db_path)

        self.assertIsNone(db.get_template(self.db_path, "ARC_RES"))
        self.assertEqual(db.list_executions(self.db_path), [])

        restore_result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path,
        )
        self.assertTrue(restore_result["success"], f"恢复失败: {restore_result.get('error')}")

        restored_tpl = templates_mod.get_template(self.db_path, self.config, "ARC_RES")
        self.assertIsNotNone(restored_tpl)
        self.assertEqual(restored_tpl["name"], "ARC_RES")
        self.assertEqual(restored_tpl["description"], "归档恢复测试")
        self.assertEqual(restored_tpl["version"], 1)

        new_eid = restore_result["execution_id"]
        execution = db.get_execution(self.db_path, new_eid)
        self.assertIsNotNone(execution)
        self.assertEqual(execution["template_name"], "ARC_RES")
        self.assertEqual(execution["template_version"], 1)
        self.assertEqual(execution["operator"], "resume_op")
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["steps_done"], 2)

        steps = db.get_steps(self.db_path, new_eid)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[1]["status"], "done")

    def test_template_show_after_restore(self):
        """恢复后 template-show 能看到模板和执行记录."""
        _, manifest_path = self._setup_and_archive("SHOW_RES")
        os.remove(self.db_path)
        db.init_db(self.db_path)

        archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path,
        )

        rc = self._run_cli("template-show", "SHOW_RES")
        self.assertEqual(rc, 0)

    def test_resume_after_restore_interrupted(self):
        """恢复中断的执行后，template-run --resume 能接着跑."""
        self._save_template(
            "RESUME_RESTORE",
            steps=[
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ],
        )
        template = templates_mod.get_template(self.db_path, self.config, "RESUME_RESTORE")
        allowed = cfg.get_allowed_statuses(self.config)

        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed, auto_archive=True,
        )
        self.assertFalse(r1["success"])
        self.assertEqual(r1["steps_done"], 1)
        self.assertEqual(r1["steps_failed"], 1)
        manifest_path = r1["archive_path"]

        os.remove(self.db_path)
        db.init_db(self.db_path)
        self.assertFalse(os.path.exists(self.db_path.replace(".db", "_notexist.db")))

        restore_result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path,
        )
        self.assertTrue(restore_result["success"])

        self._import(self._make_batch("after_restore"))

        rc = self._run_cli("template-run", "RESUME_RESTORE", "--resume")
        self.assertEqual(rc, 0)

        new_tpl = templates_mod.get_template(self.db_path, self.config, "RESUME_RESTORE")
        execs = db.list_executions(self.db_path, template_id=new_tpl["id"])
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["status"], "completed")
        self.assertEqual(execs[0]["steps_done"], 2)
        self.assertEqual(execs[0]["steps_failed"], 0)

    def test_restore_metadata_alignment(self):
        """恢复后导出的 summary/differences 文件命名、模板版本、日志与原执行一致."""
        self._import(self._make_batch("meta"))
        self._save_template(
            "META_RES",
            steps=[
                {"action": "export", "type": "summary"},
                {"action": "export", "type": "differences"},
            ],
            export_fields=["id", "location", "sku", "status"],
            description="元数据一致性测试",
        )
        template = templates_mod.get_template(self.db_path, self.config, "META_RES")
        tpl_id_orig = template["id"]
        tpl_ver_orig = template["version"]
        allowed = cfg.get_allowed_statuses(self.config)

        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]
        orig_files = set(os.listdir(self.output_dir))
        orig_export_logs = db.get_operation_logs(self.db_path, action_type="export")

        os.remove(self.db_path)
        db.init_db(self.db_path)

        restore_result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path,
        )
        self.assertTrue(restore_result["success"])

        restored_tpl = templates_mod.get_template(self.db_path, self.config, "META_RES")
        self.assertEqual(restored_tpl["version"], tpl_ver_orig)
        self.assertEqual(restored_tpl["description"], "元数据一致性测试")
        self.assertEqual(restored_tpl["export_fields"], ["id", "location", "sku", "status"])

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        for ef in manifest["export_files"]:
            self.assertIn(ef["filename"], orig_files)
            self.assertEqual(ef["template_name"], "META_RES")
            self.assertEqual(ef["template_version"], tpl_ver_orig)

        self.assertEqual(len(orig_export_logs), 2)
        for log in orig_export_logs:
            ad = log["action_data"]
            self.assertEqual(ad["template_name"], "META_RES")
            self.assertEqual(ad["template_version"], tpl_ver_orig)
            self.assertEqual(ad["template_id"], tpl_id_orig)

        new_eid = restore_result["execution_id"]
        steps = db.get_steps(self.db_path, new_eid)
        for sr in steps:
            res = sr.get("result") or {}
            if res.get("template_name"):
                self.assertEqual(res["template_name"], "META_RES")
                self.assertEqual(res["template_version"], tpl_ver_orig)

    def test_restore_corrupt_manifest_rejected(self):
        """损坏的归档文件应被拒绝，不污染数据库."""
        bad_path = os.path.join(self.tmpdir, "bad_manifest.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({"$schema": "wrong_schema", "data": "nope"}, f)

        result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, bad_path,
        )
        self.assertFalse(result["success"])
        self.assertIn("schema", result.get("error", ""))

        missing = os.path.join(self.tmpdir, "no_such_file.json")
        result2 = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, missing,
        )
        self.assertFalse(result2["success"])


class TestArchiveConflictResolution(_BaseTest):
    """冲突检测与处理：模板升级、导出文件存在、激活方案不一致."""

    def test_detect_template_upgraded_conflict(self):
        """同名模板已升级（版本或内容不同）时检测到冲突."""
        self._import(self._make_batch())
        self._save_template("CON_TPL", steps=[{"action": "export", "type": "summary"}],
                            description="v1")
        template = templates_mod.get_template(self.db_path, self.config, "CON_TPL")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        self._save_template("CON_TPL", steps=[{"action": "export", "type": "differences"}],
                            description="v2", force=True)
        v2 = templates_mod.get_template(self.db_path, self.config, "CON_TPL")
        self.assertEqual(v2["version"], 2)

        conflicts = archive_mod.detect_restore_conflicts(
            self.db_path, self.config,
            archive_mod.load_manifest(manifest_path)["manifest"],
        )
        conflict_types = [c["type"] for c in conflicts]
        self.assertIn("template_upgraded", conflict_types)

    def test_detect_export_file_exists_conflict(self):
        """归档中记录的导出文件已存在时检测到冲突."""
        self._import(self._make_batch())
        self._save_template("CON_FILE", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "CON_FILE")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        conflicts = archive_mod.detect_restore_conflicts(
            self.db_path, self.config,
            archive_mod.load_manifest(manifest_path)["manifest"],
        )
        conflict_types = [c["type"] for c in conflicts]
        self.assertIn("export_file_exists", conflict_types)

    def test_detect_active_plan_mismatch(self):
        """当前激活方案与归档记录不一致时检测到冲突."""
        cfg.set_active_plan(self.config, "plan_old")
        cfg.save_runtime_state(self.config)
        self._import(self._make_batch())
        self._save_template("CON_PLAN", steps=[{"action": "export", "type": "summary"}])
        template = templates_mod.get_template(self.db_path, self.config, "CON_PLAN")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        cfg.set_active_plan(self.config, "plan_new")
        cfg.save_runtime_state(self.config)

        conflicts = archive_mod.detect_restore_conflicts(
            self.db_path, self.config,
            archive_mod.load_manifest(manifest_path)["manifest"],
        )
        conflict_types = [c["type"] for c in conflicts]
        self.assertIn("active_plan_mismatch", conflict_types)

    def test_conflict_abort_on_template_upgrade(self):
        """模板升级冲突时 abort 策略直接中止恢复."""
        self._import(self._make_batch())
        self._save_template("CON_ABORT", steps=[{"action": "export", "type": "summary"}],
                            description="v1")
        template = templates_mod.get_template(self.db_path, self.config, "CON_ABORT")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        self._save_template("CON_ABORT", steps=[{"action": "list"}],
                            description="v2", force=True)

        result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path, conflict_resolution="abort",
        )
        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        self.assertIsNotNone(result.get("conflicts"))

    def test_conflict_save_as_on_template_upgrade(self):
        """模板升级冲突时 save-as 策略另存为 <name>_restored."""
        self._import(self._make_batch())
        self._save_template("CON_SAVEAS", steps=[{"action": "export", "type": "summary"}],
                            description="orig v1")
        template = templates_mod.get_template(self.db_path, self.config, "CON_SAVEAS")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        self._save_template("CON_SAVEAS", steps=[{"action": "list"}],
                            description="upgraded v2", force=True)

        result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path, conflict_resolution="save-as",
        )
        self.assertTrue(result["success"], f"save-as 应成功: {result.get('error')}")
        self.assertEqual(result.get("template_action"), "save_as")

        restored_name = result.get("name")
        self.assertTrue(restored_name.startswith("CON_SAVEAS_restored"))

        v2 = templates_mod.get_template(self.db_path, self.config, "CON_SAVEAS")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["description"], "upgraded v2")

        restored = templates_mod.get_template(self.db_path, self.config, restored_name)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["version"], 1)
        self.assertIn("orig v1", restored.get("description", ""))

    def test_save_as_handles_name_collision(self):
        """多次 save-as 时 _restored, _restored1, _restored2 避免重名."""
        self._import(self._make_batch())
        self._save_template("COL", steps=[{"action": "export", "type": "summary"}],
                            description="v1")
        template = templates_mod.get_template(self.db_path, self.config, "COL")
        allowed = cfg.get_allowed_statuses(self.config)
        r1 = batch_mod.run_template(
            self.db_path, self.config, template, self.output_dir,
            allowed_statuses=allowed,
        )
        manifest_path = r1["archive_path"]

        self._save_template("COL", steps=[{"action": "list"}], force=True)
        templates_mod.save_template(
            self.db_path, self.config, "COL_restored",
            steps=[{"action": "list"}], description="占位",
        )

        result = archive_mod.restore_execution_from_manifest(
            self.db_path, self.config, manifest_path, conflict_resolution="save-as",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "COL_restored1")


class TestArchiveCLI(_BaseTest):
    """CLI 命令：template-export-execution / template-restore-execution / 重启续跑."""

    def test_cli_export_execution(self):
        """template-export-execution 成功导出归档."""
        self._import(self._make_batch())
        self._run_cli("template-save", "CLI_ARC",
                      "--steps", "list,export:summary",
                      "-d", "cli archive test")
        rc = self._run_cli("template-run", "CLI_ARC")
        self.assertEqual(rc, 0)

        out = os.path.join(self.tmpdir, "cli_manifest.json")
        rc = self._run_cli("template-export-execution", "CLI_ARC", "-o", out)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out))

        with open(out, "r", encoding="utf-8") as f:
            m = json.load(f)
        self.assertEqual(m["execution_meta"]["template_name"], "CLI_ARC")

    def test_cli_export_execution_by_id(self):
        """template-export-execution -e <id> 按 execution_id 导出."""
        self._import(self._make_batch())
        self._run_cli("template-save", "CLI_EID", "--steps", "export:summary")
        self._run_cli("template-run", "CLI_EID")

        template = templates_mod.get_template(self.db_path, self.config, "CLI_EID")
        execs = db.list_executions(self.db_path, template_id=template["id"])
        eid = execs[0]["id"]

        out = os.path.join(self.tmpdir, "by_id.json")
        rc = self._run_cli("template-export-execution", "-e", str(eid), "-o", out)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out))

    def test_cli_restore_execution(self):
        """template-restore-execution 从清单恢复."""
        self._import(self._make_batch())
        self._run_cli("template-save", "CLI_REST", "--steps", "list,export:summary",
                      "-d", "restore from cli")
        self._run_cli("template-run", "CLI_REST")

        manifest_path = os.path.join(self.tmpdir, "cli_restore.json")
        self._run_cli("template-export-execution", "CLI_REST", "-o", manifest_path)

        os.remove(self.db_path)
        db.init_db(self.db_path)

        rc = self._run_cli("template-restore-execution", manifest_path)
        self.assertEqual(rc, 0)

        t = templates_mod.get_template(self.db_path, self.config, "CLI_REST")
        self.assertIsNotNone(t)
        self.assertEqual(t["description"], "restore from cli")

        self.assertEqual(self._run_cli("template-show", "CLI_REST"), 0)

    def test_cli_restore_conflict_abort_exit_code(self):
        """冲突时 template-restore-execution 默认 abort 返回非 0."""
        self._import(self._make_batch())
        self._run_cli("template-save", "CLI_CNF", "--steps", "export:summary",
                      "-d", "v1")
        self._run_cli("template-run", "CLI_CNF")

        manifest_path = os.path.join(self.tmpdir, "conflict.json")
        self._run_cli("template-export-execution", "CLI_CNF", "-o", manifest_path)

        self._run_cli("template-save", "CLI_CNF", "--steps", "list",
                      "-d", "v2", "--force")

        rc = self._run_cli("template-restore-execution", manifest_path)
        self.assertNotEqual(rc, 0)

    def test_cli_restore_conflict_save_as(self):
        """template-restore-execution --conflict save-as 成功返回 0."""
        self._import(self._make_batch())
        self._run_cli("template-save", "CLI_SAVEAS", "--steps", "export:summary",
                      "-d", "v1")
        self._run_cli("template-run", "CLI_SAVEAS")

        manifest_path = os.path.join(self.tmpdir, "saveas.json")
        self._run_cli("template-export-execution", "CLI_SAVEAS", "-o", manifest_path)

        self._run_cli("template-save", "CLI_SAVEAS", "--steps", "list",
                      "-d", "v2", "--force")

        rc = self._run_cli("template-restore-execution", manifest_path,
                           "--conflict", "save-as")
        self.assertEqual(rc, 0)

        self.assertIsNotNone(templates_mod.get_template(
            self.db_path, self.config, "CLI_SAVEAS_restored",
        ))

    def test_cli_restart_delete_db_restore_and_resume(self):
        """完整链路：执行中断 → 导出归档 → 删 DB → 恢复 → 续跑成功."""
        self._run_cli(
            "template-save", "FULLCHAIN",
            "--steps", "export:summary,export:differences",
            "-d", "full chain test",
        )
        rc1 = self._run_cli("template-run", "FULLCHAIN")
        self.assertEqual(rc1, 2)

        manifest_path = os.path.join(self.tmpdir, "fullchain_manifest.json")
        self._run_cli("template-export-execution", "FULLCHAIN", "-o", manifest_path)

        os.remove(self.db_path)
        db.init_db(self.db_path)

        rc2 = self._run_cli("template-restore-execution", manifest_path)
        self.assertEqual(rc2, 0)

        self._import(self._make_batch("after_restart"))

        rc3 = self._run_cli("template-run", "FULLCHAIN", "--resume")
        self.assertEqual(rc3, 0)

        self.assertEqual(self._run_cli("template-show", "FULLCHAIN"), 0)


if __name__ == "__main__":
    unittest.main()
