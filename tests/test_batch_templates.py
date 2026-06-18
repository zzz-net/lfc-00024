"""批量任务模板回归测试 - 覆盖跨重启恢复、导入导出、权限/路径失败、撤销."""
import json
import os
import shutil
import stat
import tempfile
import unittest

from inventory_audit import config as cfg
from inventory_audit import db
from inventory_audit import batch_templates as bt


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


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_bt_test_")
        self.config = _make_config(self.tmpdir)
        self.db_path = cfg.get_db_path(self.config)
        cfg.ensure_dirs(self.config)
        db.init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestBatchTemplatesCRUD(_BaseTest):
    """批量任务模板基础 CRUD 测试."""

    def test_save_and_get_template(self):
        """保存模板并查询，字段完整还原."""
        result = bt.save_batch_template(
            self.db_path,
            self.config,
            name="daily_pending",
            description="日常待处理",
            execution_params={"status": "pending", "batch_id": 1},
            env_whitelist=["PYTHONPATH", "HOME"],
            export_options={"include_sources": True, "encoding": "utf-8-sig"},
            conflict_strategy="save-as",
            operator="tester",
        )
        self.assertEqual(result["status"], "created")
        self.assertGreater(result["template_id"], 0)

        tpl = bt.get_batch_template(self.db_path, self.config, "daily_pending")
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl["name"], "daily_pending")
        self.assertEqual(tpl["description"], "日常待处理")
        self.assertEqual(tpl["conflict_strategy"], "save-as")
        self.assertEqual(tpl["disabled"], False)
        self.assertEqual(tpl["execution_params"]["status"], "pending")
        self.assertEqual(tpl["execution_params"]["batch_id"], 1)
        self.assertEqual(set(tpl["env_whitelist"]), {"PYTHONPATH", "HOME"})
        self.assertEqual(tpl["export_options"]["include_sources"], True)
        self.assertIsNotNone(tpl.get("content_hash"))

    def test_save_existing_template_modifies(self):
        """同名二次保存应触发 modify 历史，可被 undo."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="tpl1", description="v1",
            execution_params={"k": 1}, conflict_strategy="abort",
            operator="u1",
        )
        result = bt.save_batch_template(
            self.db_path, self.config,
            name="tpl1", description="v2",
            execution_params={"k": 2}, conflict_strategy="abort",
            operator="u1",
        )
        self.assertEqual(result["status"], "modified")

        tpl = bt.get_batch_template(self.db_path, self.config, "tpl1")
        self.assertEqual(tpl["description"], "v2")
        self.assertEqual(tpl["execution_params"]["k"], 2)

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "tpl1", operator="u1",
        )
        self.assertEqual(undo["status"], "restored")

        tpl = bt.get_batch_template(self.db_path, self.config, "tpl1")
        self.assertEqual(tpl["description"], "v1")
        self.assertEqual(tpl["execution_params"]["k"], 1)

    def test_save_identical_content_is_noop(self):
        """内容完全一致时保存返回 unchanged，不写 history."""
        r1 = bt.save_batch_template(
            self.db_path, self.config,
            name="tpl_same", description="same",
            execution_params={"k": 1}, conflict_strategy="abort",
        )
        self.assertEqual(r1["status"], "created")

        r2 = bt.save_batch_template(
            self.db_path, self.config,
            name="tpl_same", description="same",
            execution_params={"k": 1}, conflict_strategy="abort",
        )
        self.assertEqual(r2["status"], "unchanged")

        history = db.get_last_batch_template_history(self.db_path, "tpl_same")
        self.assertIsNotNone(history)
        self.assertEqual(history["action"], "create")

    def test_list_and_disable_enable(self):
        """模板列表、禁用、启用流程."""
        bt.save_batch_template(
            self.db_path, self.config, name="t1", conflict_strategy="abort",
        )
        bt.save_batch_template(
            self.db_path, self.config, name="t2", disabled=True, conflict_strategy="abort",
        )

        active = bt.list_batch_templates(self.db_path, include_disabled=False)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "t1")

        all_tpls = bt.list_batch_templates(self.db_path, include_disabled=True)
        self.assertEqual(len(all_tpls), 2)

        bt.set_batch_template_disabled(self.db_path, self.config, "t1", True, operator="u1")
        active = bt.list_batch_templates(self.db_path, include_disabled=False)
        self.assertEqual(len(active), 0)

        bt.set_batch_template_disabled(self.db_path, self.config, "t1", False, operator="u1")
        active = bt.list_batch_templates(self.db_path, include_disabled=False)
        self.assertEqual(len(active), 1)

    def test_copy_template(self):
        """复制模板：字段内容一致，名称独立."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="src", description="source",
            execution_params={"a": 1},
            env_whitelist=["X"],
            export_options={"y": True},
            conflict_strategy="overwrite",
        )
        result = bt.copy_batch_template(
            self.db_path, self.config, "src", "dst", operator="u1",
        )
        self.assertEqual(result["status"], "copied")

        dst = bt.get_batch_template(self.db_path, self.config, "dst")
        self.assertEqual(dst["description"], "source")
        self.assertEqual(dst["execution_params"], {"a": 1})
        self.assertEqual(dst["env_whitelist"], ["X"])
        self.assertEqual(dst["export_options"], {"y": True})
        self.assertEqual(dst["conflict_strategy"], "overwrite")
        self.assertEqual(dst["disabled"], False)

    def test_delete_and_undo(self):
        """删除模板后可通过 undo 恢复，包括 JSON 落盘文件."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="to_delete", description="will be deleted",
            conflict_strategy="abort",
        )
        json_path = bt._get_template_json_path(self.config, "to_delete")
        self.assertTrue(os.path.exists(json_path))

        result = bt.delete_batch_template(
            self.db_path, self.config, "to_delete", operator="u1",
        )
        self.assertEqual(result["status"], "deleted")
        self.assertIsNone(bt.get_batch_template(self.db_path, self.config, "to_delete"))
        self.assertFalse(os.path.exists(json_path))

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "to_delete", operator="u1",
        )
        self.assertEqual(undo["status"], "restored")
        restored = bt.get_batch_template(self.db_path, self.config, "to_delete")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["description"], "will be deleted")
        self.assertTrue(os.path.exists(json_path))


class TestBatchTemplatesUndo(_BaseTest):
    """撤销最近一次变更覆盖更多场景."""

    def test_undo_create_removes_template(self):
        """撤销 create 操作应删除模板."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="tmp", description="temp", conflict_strategy="abort", operator="u1",
        )
        self.assertIsNotNone(bt.get_batch_template(self.db_path, self.config, "tmp"))

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "tmp", operator="u1",
        )
        self.assertEqual(undo["status"], "deleted")
        self.assertIsNone(bt.get_batch_template(self.db_path, self.config, "tmp"))

    def test_undo_disable_flips_back(self):
        """撤销 disable 操作应重新启用."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="d1", conflict_strategy="abort", operator="u1",
        )
        bt.set_batch_template_disabled(self.db_path, self.config, "d1", True, operator="u1")

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "d1", operator="u1",
        )
        self.assertEqual(undo["status"], "restored")
        tpl = bt.get_batch_template(self.db_path, self.config, "d1")
        self.assertEqual(tpl["disabled"], False)

    def test_undo_enable_flips_back(self):
        """撤销 enable 操作应重新禁用."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="e1", disabled=True, conflict_strategy="abort", operator="u1",
        )
        bt.set_batch_template_disabled(self.db_path, self.config, "e1", False, operator="u1")

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "e1", operator="u1",
        )
        self.assertEqual(undo["status"], "restored")
        tpl = bt.get_batch_template(self.db_path, self.config, "e1")
        self.assertEqual(tpl["disabled"], True)

    def test_undo_no_history_returns_not_found(self):
        """没有历史记录的模板返回 not_found."""
        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "ghost", operator="u1",
        )
        self.assertEqual(undo["status"], "not_found")


class TestBatchTemplatesRecovery(_BaseTest):
    """跨重启 / DB 丢失后的恢复测试."""

    def test_json_fallback_recovers_template(self):
        """DB 被清空但 JSON 落盘还在时，get_batch_template 能从 JSON 回补到 DB."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="recover_me", description="before crash",
            execution_params={"step": "pre-crash"},
            env_whitelist=["A", "B"],
            export_options={"format": "json"},
            conflict_strategy="overwrite",
            operator="u1",
        )
        json_path = bt._get_template_json_path(self.config, "recover_me")
        self.assertTrue(os.path.exists(json_path))

        with open(json_path, "r", encoding="utf-8") as f:
            json_snapshot = json.load(f)

        # 模拟 DB 丢失（删除 db 文件并重建）
        os.remove(self.db_path)
        db.init_db(self.db_path)
        self.assertIsNone(db.get_batch_template(self.db_path, "recover_me"))

        # get_batch_template 应从 JSON 回补
        tpl = bt.get_batch_template(self.db_path, self.config, "recover_me")
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl["name"], "recover_me")
        self.assertEqual(tpl["description"], "before crash")
        self.assertEqual(tpl["execution_params"], {"step": "pre-crash"})
        self.assertEqual(tpl["env_whitelist"], ["A", "B"])
        self.assertEqual(tpl["export_options"], {"format": "json"})
        self.assertEqual(tpl["conflict_strategy"], "overwrite")

        # 回补后 DB 中也应该有数据
        tpl_db = db.get_batch_template(self.db_path, "recover_me")
        self.assertIsNotNone(tpl_db)

        # JSON 内容应保持不变
        with open(json_path, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), json_snapshot)

    def test_list_triggers_recovery_from_json(self):
        """list_batch_templates 时也会扫描 JSON 目录回补丢失的模板."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="l1", conflict_strategy="abort",
        )
        bt.save_batch_template(
            self.db_path, self.config,
            name="l2", description="second", conflict_strategy="abort",
        )

        os.remove(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(len(bt.list_batch_templates(self.db_path)), 0)

        # JSON 落盘都在 audit_data/batch_templates/
        bt_dir = os.path.join(self.config["export"]["output_dir"].rsplit(os.sep, 1)[0], "batch_templates")
        self.assertTrue(os.path.isdir(bt_dir))
        self.assertEqual(len(os.listdir(bt_dir)), 2)

        all_tpls = bt.list_batch_templates(self.db_path, include_disabled=True, config=self.config)
        names = sorted(t["name"] for t in all_tpls)
        self.assertEqual(names, ["l1", "l2"])


class TestBatchTemplatesImportExport(_BaseTest):
    """导入导出和三种冲突策略."""

    def _make_template(self, name, desc="default", extra_params=None):
        params = {"status": "pending"}
        if extra_params:
            params.update(extra_params)
        bt.save_batch_template(
            self.db_path, self.config,
            name=name, description=desc,
            execution_params=params,
            env_whitelist=["PYTHONPATH"],
            export_options={"include_sources": True},
            conflict_strategy="abort",
            operator="u1",
        )

    def test_export_and_import_new(self):
        """导出 → 删除 → 导入：模板完全还原."""
        self._make_template("ex1", desc="exported", extra_params={"x": 1})

        export_path = os.path.join(self.tmpdir, "ex1.json")
        exp = bt.export_batch_template(self.db_path, self.config, "ex1", export_path)
        self.assertEqual(exp["status"], "exported")
        self.assertTrue(os.path.exists(export_path))

        with open(export_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["name"], "ex1")
        self.assertEqual(payload["description"], "exported")
        self.assertIn("schema_version", payload)
        self.assertIn("content_hash", payload)

        bt.delete_batch_template(self.db_path, self.config, "ex1", operator="u1")
        self.assertIsNone(bt.get_batch_template(self.db_path, self.config, "ex1"))

        imp = bt.import_batch_template(
            self.db_path, self.config, export_path, conflict="abort", operator="u2",
        )
        self.assertEqual(imp["status"], "created")
        self.assertEqual(imp["resolved_name"], "ex1")

        tpl = bt.get_batch_template(self.db_path, self.config, "ex1")
        self.assertEqual(tpl["description"], "exported")
        self.assertEqual(tpl["execution_params"], {"status": "pending", "x": 1})
        self.assertEqual(tpl["env_whitelist"], ["PYTHONPATH"])
        self.assertEqual(tpl["export_options"], {"include_sources": True})

    def test_import_abort_on_conflict(self):
        """abort 策略：重名且内容不同时中止，不修改任何数据."""
        self._make_template("c1", desc="v1", extra_params={"v": 1})
        t_before = bt.get_batch_template(self.db_path, self.config, "c1")

        # 造一个内容不同的 JSON
        payload = {
            "schema_version": 1,
            "name": "c1",
            "description": "v2",
            "disabled": False,
            "execution_params": {"status": "pending", "v": 2},
            "env_whitelist": ["PYTHONPATH"],
            "export_options": {"include_sources": True},
            "conflict_strategy": "abort",
            "content_hash": "fake_hash_different_from_v1",
        }
        import_path = os.path.join(self.tmpdir, "conflict.json")
        with open(import_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        imp = bt.import_batch_template(
            self.db_path, self.config, import_path, conflict="abort", operator="u1",
        )
        self.assertEqual(imp["status"], "conflict_aborted")

        t_after = bt.get_batch_template(self.db_path, self.config, "c1")
        self.assertEqual(t_after["description"], t_before["description"])
        self.assertEqual(t_after["execution_params"]["v"], 1)

    def test_import_save_as_on_conflict(self):
        """save-as 策略：重名时自动另存为 _2、_3."""
        self._make_template("sa", desc="original", extra_params={"v": 1})

        payload = {
            "schema_version": 1,
            "name": "sa",
            "description": "imported_v2",
            "disabled": False,
            "execution_params": {"status": "pending", "v": 99},
            "env_whitelist": ["PYTHONPATH"],
            "export_options": {"include_sources": True},
            "conflict_strategy": "abort",
            "content_hash": "somehash",
        }
        import_path = os.path.join(self.tmpdir, "saveas.json")
        with open(import_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        imp = bt.import_batch_template(
            self.db_path, self.config, import_path, conflict="save-as", operator="u1",
        )
        self.assertEqual(imp["status"], "created")
        self.assertEqual(imp["resolved_name"], "sa_2")

        orig = bt.get_batch_template(self.db_path, self.config, "sa")
        self.assertEqual(orig["description"], "original")
        self.assertEqual(orig["execution_params"]["v"], 1)

        new = bt.get_batch_template(self.db_path, self.config, "sa_2")
        self.assertEqual(new["description"], "imported_v2")
        self.assertEqual(new["execution_params"]["v"], 99)

        # 再导入一次，应生成 sa_3
        imp2 = bt.import_batch_template(
            self.db_path, self.config, import_path, conflict="save-as", operator="u1",
        )
        self.assertEqual(imp2["resolved_name"], "sa_3")

    def test_import_overwrite_on_conflict(self):
        """overwrite 策略：重名时覆盖并记录 modify 历史，可 undo."""
        self._make_template("ow", desc="old", extra_params={"v": 1})

        payload = {
            "schema_version": 1,
            "name": "ow",
            "description": "new",
            "disabled": False,
            "execution_params": {"status": "pending", "v": 2},
            "env_whitelist": ["PYTHONPATH"],
            "export_options": {"include_sources": True},
            "conflict_strategy": "abort",
            "content_hash": "somehash",
        }
        import_path = os.path.join(self.tmpdir, "over.json")
        with open(import_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        imp = bt.import_batch_template(
            self.db_path, self.config, import_path, conflict="overwrite", operator="u1",
        )
        self.assertEqual(imp["status"], "overwritten")
        self.assertEqual(imp["resolved_name"], "ow")

        tpl = bt.get_batch_template(self.db_path, self.config, "ow")
        self.assertEqual(tpl["description"], "new")
        self.assertEqual(tpl["execution_params"]["v"], 2)

        undo = bt.undo_last_batch_template_change(
            self.db_path, self.config, "ow", operator="u1",
        )
        self.assertEqual(undo["status"], "restored")
        restored = bt.get_batch_template(self.db_path, self.config, "ow")
        self.assertEqual(restored["description"], "old")
        self.assertEqual(restored["execution_params"]["v"], 1)

    def test_import_same_content_is_unchanged(self):
        """内容完全一致时，三种策略都返回 unchanged."""
        self._make_template("same", desc="same", extra_params={"v": 7})
        export_path = os.path.join(self.tmpdir, "same.json")
        bt.export_batch_template(self.db_path, self.config, "same", export_path)

        for strategy in ("abort", "save-as", "overwrite"):
            imp = bt.import_batch_template(
                self.db_path, self.config, export_path,
                conflict=strategy, operator="u1",
            )
            self.assertEqual(imp["status"], "unchanged", f"strategy={strategy}")

    def test_import_invalid_conflict_strategy(self):
        """无效冲突策略应抛错."""
        payload = {"schema_version": 1, "name": "x", "execution_params": {},
                   "env_whitelist": [], "export_options": {}, "conflict_strategy": "abort"}
        p = os.path.join(self.tmpdir, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        with self.assertRaises(ValueError):
            bt.import_batch_template(
                self.db_path, self.config, p, conflict="bogus", operator="u1",
            )


class TestBatchTemplatesPathValidation(_BaseTest):
    """路径和权限校验失败场景."""

    def test_export_non_writable_dir_fails(self):
        """输出路径非法时返回 error（目录路径而非文件路径）."""
        bt.save_batch_template(
            self.db_path, self.config,
            name="perm_tpl", conflict_strategy="abort",
        )

        # 传一个已存在的目录作为输出文件路径
        ro_dir = os.path.join(self.tmpdir, "some_dir")
        os.makedirs(ro_dir, exist_ok=True)

        result = bt.export_batch_template(
            self.db_path, self.config, "perm_tpl", ro_dir,
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

    def test_import_nonexistent_file_fails(self):
        """导入不存在的文件返回 error."""
        ghost = os.path.join(self.tmpdir, "does_not_exist.json")
        imp = bt.import_batch_template(
            self.db_path, self.config, ghost, conflict="abort",
        )
        self.assertEqual(imp["status"], "error")
        self.assertIn("不存在", imp.get("error", ""))

    def test_import_malformed_json_fails(self):
        """损坏的 JSON 返回 error."""
        bad = os.path.join(self.tmpdir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ not json :")

        imp = bt.import_batch_template(
            self.db_path, self.config, bad, conflict="abort",
        )
        self.assertEqual(imp["status"], "error")
        self.assertIn("JSON", imp.get("error", ""))

    def test_import_missing_required_fields_fails(self):
        """缺少必填字段的导入返回 error（缺少 name 字段）."""
        bad = os.path.join(self.tmpdir, "incomplete.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"description": "missing name field"}, f)

        imp = bt.import_batch_template(
            self.db_path, self.config, bad, conflict="abort",
        )
        self.assertEqual(imp["status"], "error")
        self.assertIn("missing", imp.get("error", "").lower())

    def test_validate_batch_template_structure(self):
        """validate_batch_template 校验数据结构."""
        ok, msg = bt.validate_batch_template({
            "name": "tpl_ok",
            "execution_params": {"a": 1},
            "env_whitelist": ["X"],
            "export_options": {"y": True},
            "conflict_strategy": "abort",
        })
        self.assertTrue(ok, msg)

        ok, msg = bt.validate_batch_template({
            "name": "tpl_bad",
            "execution_params": "not_a_dict",
            "env_whitelist": ["X"],
            "export_options": {},
            "conflict_strategy": "abort",
        })
        self.assertFalse(ok)
        self.assertIn("execution_params", msg)

        ok, msg = bt.validate_batch_template({
            "name": "tpl_bad",
            "execution_params": {},
            "env_whitelist": "not_a_list",
            "export_options": {},
            "conflict_strategy": "abort",
        })
        self.assertFalse(ok)
        self.assertIn("env_whitelist", msg)

        ok, msg = bt.validate_batch_template({
            "name": "tpl_bad",
            "execution_params": {},
            "env_whitelist": [],
            "export_options": "not_a_dict",
            "conflict_strategy": "abort",
        })
        self.assertFalse(ok)
        self.assertIn("export_options", msg)

        ok, msg = bt.validate_batch_template({
            "name": "tpl_bad",
            "execution_params": {},
            "env_whitelist": [],
            "export_options": {},
            "conflict_strategy": "bogus",
        })
        self.assertFalse(ok)
        self.assertIn("conflict_strategy", msg)


class TestBatchTemplatesContentHash(_BaseTest):
    """内容哈希指纹一致性测试."""

    def test_same_content_same_hash(self):
        """相同内容产生相同哈希."""
        h1 = bt.compute_batch_content_hash(
            {"status": "pending", "a": 1}, ["X", "Y"], {"k": True}, "abort",
        )
        h2 = bt.compute_batch_content_hash(
            {"status": "pending", "a": 1}, ["X", "Y"], {"k": True}, "abort",
        )
        self.assertEqual(h1, h2)

    def test_different_content_different_hash(self):
        """不同内容产生不同哈希（覆盖各个字段）."""
        base = ({"a": 1}, ["X"], {"k": True}, "abort")
        h_base = bt.compute_batch_content_hash(*base)

        self.assertNotEqual(h_base, bt.compute_batch_content_hash({"a": 2}, ["X"], {"k": True}, "abort"))
        self.assertNotEqual(h_base, bt.compute_batch_content_hash({"a": 1}, ["Y"], {"k": True}, "abort"))
        self.assertNotEqual(h_base, bt.compute_batch_content_hash({"a": 1}, ["X"], {"k": False}, "abort"))
        self.assertNotEqual(h_base, bt.compute_batch_content_hash({"a": 1}, ["X"], {"k": True}, "overwrite"))

    def test_key_order_independent(self):
        """字典 key 顺序不影响哈希."""
        h1 = bt.compute_batch_content_hash(
            {"a": 1, "b": 2, "c": 3}, [], {}, "abort",
        )
        h2 = bt.compute_batch_content_hash(
            {"c": 3, "a": 1, "b": 2}, [], {}, "abort",
        )
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
