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

    def _run_cli(self, *argv):
        return cli.main(["-c", self.cfg_file] + list(argv))

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


if __name__ == "__main__":
    unittest.main()
