"""复核方案与操作回放测试 - 覆盖重启续用、方案切换导入、冲突处理、撤销后回放."""
import csv
import json
import os
import shutil
import tempfile
import unittest

from inventory_audit import cli
from inventory_audit import config as cfg
from inventory_audit import db
from inventory_audit import exporter
from inventory_audit import importer
from inventory_audit import merger
from inventory_audit import plans as plans_mod
from inventory_audit import replay as replay_mod
from inventory_audit import reviewer


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
    """把配置写入 tmpdir/config.json，供 CLI -c 参数使用."""
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
        self.tmpdir = tempfile.mkdtemp(prefix="audit_plan_")
        self.config = _make_config(self.tmpdir)
        self.db_path = cfg.get_db_path(self.config)
        cfg.ensure_dirs(self.config)
        db.init_db(self.db_path)
        cfg.save_runtime_state(self.config)
        self.cfg_file = _write_config_file(self.tmpdir, self.config)

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

    def _setup_diff(self):
        csv_path = os.path.join(self.tmpdir, "r.csv")
        _write_csv(csv_path, ["location", "sku", "expected_qty", "counted_qty"],
                   [["A-01", "SKU_R", "100", "90"]])
        self._import(csv_path)
        return db.list_differences(self.db_path)[0]["id"]

    def _cli(self, *args):
        """调用 CLI，自动带上 -c <临时目录 config.json>."""
        return cli.main(["-c", self.cfg_file, *args])


class TestPlanPersistence(_BaseTest):
    """方案持久化：重启后仍可读取，双写数据库 + JSON 落盘."""

    def test_save_plan_and_reload_via_db(self):
        """方案保存后可从数据库读取，字段完整."""
        result = plans_mod.save_plan(
            self.db_path, self.config, "morning-check",
            filter_status="pending",
            filter_location="A-",
            export_fields=["id", "location", "sku", "status", "remark"],
            remark_template="已核对,无异议",
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result["plan_id"])

        loaded = plans_mod.get_plan(self.db_path, self.config, "morning-check")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["filter_status"], "pending")
        self.assertEqual(loaded["filter_location"], "A-")
        self.assertEqual(loaded["remark_template"], "已核对,无异议")
        self.assertEqual(loaded["export_fields"],
                         ["id", "location", "sku", "status", "remark"])

    def test_plan_json_fallback_recovers_after_db_reset(self):
        """即使数据库被清空（只剩 plans/*.json），仍能还原方案."""
        plans_mod.save_plan(
            self.db_path, self.config, "fallback-plan",
            filter_status="ignored",
            remark_template="tpl",
        )
        db_dir = os.path.dirname(self.db_path)
        json_path = os.path.join(db_dir, "plans", "fallback-plan.json")
        self.assertTrue(os.path.exists(json_path), "方案必须 JSON 双写落盘")

        os.remove(self.db_path)
        db.init_db(self.db_path)
        self.assertEqual(plans_mod.list_plans(self.db_path), [])

        loaded = plans_mod.get_plan(self.db_path, self.config, "fallback-plan")
        self.assertIsNotNone(loaded, "JSON 落盘必须能回补数据库")
        self.assertEqual(loaded["filter_status"], "ignored")

    def test_active_plan_persists_across_reload(self):
        """active_plan 落盘 runtime_state.json，重启 load_config 能读回."""
        plans_mod.save_plan(self.db_path, self.config, "persist-plan")
        cfg.set_active_plan(self.config, "persist-plan")
        cfg.set_operator(self.config, "alice")

        db_dir = os.path.dirname(self.db_path)
        runtime_path = os.path.join(db_dir, "runtime_state.json")
        self.assertTrue(os.path.exists(runtime_path))

        restored_config = _make_config(self.tmpdir)
        restored = cfg.load_config(self.cfg_file)
        self.assertEqual(restored.get("active_plan"), "persist-plan")
        self.assertEqual(restored.get("operator"), "alice")


class TestPlanSwitchDoesNotMutateOldBatches(_BaseTest):
    """切换方案再导入：旧批次 ID、名称、汇总统计不得串改."""

    def test_import_after_plan_switch_preserves_old_batch(self):
        """方案 A 下导入批次 1，切到方案 B 导入批次 2，批次 1 不变."""
        plans_mod.save_plan(self.db_path, self.config, "plan-a", filter_status="pending")
        plans_mod.save_plan(self.db_path, self.config, "plan-b", filter_status="confirmed")

        cfg.set_active_plan(self.config, "plan-a")
        csv1 = self._make_batch("1")
        r1 = self._import(csv1)
        self.assertTrue(r1["success"])
        batch1_id = r1["batch_id"]
        batch1_before = db.list_batches(self.db_path)
        summary_before = db.get_summary(self.db_path)

        cfg.set_active_plan(self.config, "plan-b")
        csv2 = self._make_batch("2")
        r2 = self._import(csv2)
        self.assertTrue(r2["success"])

        batch1_after = next(b for b in db.list_batches(self.db_path) if b["id"] == batch1_id)
        orig = next(b for b in batch1_before if b["id"] == batch1_id)
        self.assertEqual(batch1_after["batch_name"], orig["batch_name"])
        self.assertEqual(batch1_after["file_path"], orig["file_path"])
        self.assertEqual(batch1_after["file_hash"], orig["file_hash"])

        summary_after = db.get_summary(self.db_path)
        self.assertEqual(summary_after["batch_count"], summary_before["batch_count"] + 1)
        self.assertGreaterEqual(summary_after["total_differences"], summary_before["total_differences"])

    def test_plan_filters_do_not_affect_underlying_data(self):
        """方案筛选只影响 list/export 的视图，不写入差异数据."""
        plans_mod.save_plan(self.db_path, self.config, "only-a",
                            filter_location="A-",
                            export_fields=["id", "location", "sku"])
        csv_path = self._make_batch("x")
        self._import(csv_path)
        all_diffs = db.list_differences(self.db_path)
        self.assertEqual(len(all_diffs), 2)

        cfg.set_active_plan(self.config, "only-a")
        filtered = merger.get_merged_differences(
            self.db_path, status=None, location="A-", sku=None,
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(db.list_differences(self.db_path)), 2,
                         "方案筛选不能修改底层差异记录")


class TestReplayUndoAndConflicts(_BaseTest):
    """操作日志回放：状态变更、备注、撤销、导出；冲突检测三策略."""

    def test_replay_status_and_remark_restores_state(self):
        """回放 status_change + remark_change 能还原差异状态与备注（跳过 undo 日志）."""
        plans_mod.save_plan(self.db_path, self.config, "plan-r", filter_status="pending")
        plan_r = plans_mod.get_plan(self.db_path, self.config, "plan-r")
        diff_id = self._setup_diff()

        reviewer.set_status(
            self.db_path, diff_id, "confirmed", operator="bob",
            plan_id=plan_r["id"], plan_name=plan_r["name"],
            allowed_statuses=self.config["status"]["allowed"],
        )
        reviewer.set_remark(
            self.db_path, diff_id, "已盘点", operator="bob",
            plan_id=plan_r["id"], plan_name=plan_r["name"],
        )

        exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            plan=plan_r, operator="bob",
        )

        logs_before = db.get_operation_logs(self.db_path)
        self.assertGreaterEqual(len(logs_before), 3)

        reviewer.undo_last(self.db_path, operator="bob",
                           plan_id=plan_r["id"], plan_name=plan_r["name"])
        reviewer.undo_last(self.db_path, operator="bob",
                           plan_id=plan_r["id"], plan_name=plan_r["name"])
        d = db.get_difference(self.db_path, diff_id)
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["remark"], "")

        output_dir = self.config["export"]["output_dir"]
        result = replay_mod.replay_operations(
            self.db_path, output_dir,
            plan_id=plan_r["id"],
            allowed_statuses=self.config["status"]["allowed"],
            action_types=["status_change", "remark_change", "export"],
        )
        self.assertTrue(result["success"], f"回放应成功：{result}")
        replayed_actions = {r["action"] for r in result["replayed"]}
        self.assertIn("status_change", replayed_actions)
        self.assertIn("remark_change", replayed_actions)

        d2 = db.get_difference(self.db_path, diff_id)
        self.assertEqual(d2["status"], "confirmed")
        self.assertEqual(d2["remark"], "已盘点")
        self.assertGreaterEqual(len(result["exports"]), 1, "导出操作应被重新导出")

    def test_undo_then_replay(self):
        """撤销 status_change 后回放 status_change，状态还原到变更后的值."""
        diff_id = self._setup_diff()
        reviewer.set_status(
            self.db_path, diff_id, "ignored", operator="u1",
            allowed_statuses=self.config["status"]["allowed"],
        )
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "ignored")

        reviewer.undo_last(self.db_path, operator="u1")
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "pending")

        result = replay_mod.replay_operations(
            self.db_path, self.config["export"]["output_dir"],
            allowed_statuses=self.config["status"]["allowed"],
            action_types=["status_change"],
        )
        self.assertTrue(result["success"])
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "ignored")

    def test_conflict_abort_stops_replay(self):
        """同一差异被不同方案改过时，回放检测到冲突并中止."""
        plans_mod.save_plan(self.db_path, self.config, "plan-x")
        plans_mod.save_plan(self.db_path, self.config, "plan-y")
        px = plans_mod.get_plan(self.db_path, self.config, "plan-x")
        py = plans_mod.get_plan(self.db_path, self.config, "plan-y")
        diff_id = self._setup_diff()

        reviewer.set_remark(
            self.db_path, diff_id, "x 已处理", operator="alice",
            plan_id=px["id"], plan_name=px["name"],
        )

        reviewer.set_remark(
            self.db_path, diff_id, "y 覆盖", operator="bob",
            plan_id=py["id"], plan_name=py["name"],
        )

        result = replay_mod.replay_operations(
            self.db_path, self.config["export"]["output_dir"],
            plan_id=px["id"],
            allowed_statuses=self.config["status"]["allowed"],
            default_conflict_resolution=replay_mod.CONFLICT_ABORT,
            action_types=["remark_change"],
        )
        self.assertFalse(result["success"])
        self.assertIsNotNone(result["aborted"])
        self.assertIn("冲突", result["aborted"]["message"])
        self.assertIn("备注", result["aborted"]["message"])

    def test_conflict_snapshot_saves_state(self):
        """冲突发生时 snapshot 策略另存 JSON，然后跳过该条."""
        diff_id = self._setup_diff()
        plans_mod.save_plan(self.db_path, self.config, "plan-k")
        pk = plans_mod.get_plan(self.db_path, self.config, "plan-k")

        reviewer.set_status(
            self.db_path, diff_id, "confirmed", operator="k1",
            plan_id=pk["id"], plan_name=pk["name"],
            allowed_statuses=self.config["status"]["allowed"],
        )
        reviewer.set_status(
            self.db_path, diff_id, "closed", operator="k2",
            allowed_statuses=self.config["status"]["allowed"],
        )

        output_dir = self.config["export"]["output_dir"]
        result = replay_mod.replay_operations(
            self.db_path, output_dir,
            plan_id=pk["id"],
            allowed_statuses=self.config["status"]["allowed"],
            default_conflict_resolution=replay_mod.CONFLICT_SNAPSHOT,
        )
        self.assertTrue(result["success"])
        snap_items = [s for s in result["skipped"] if s.get("snapshot_path")]
        self.assertGreaterEqual(len(snap_items), 1)
        self.assertTrue(os.path.exists(snap_items[0]["snapshot_path"]))
        with open(snap_items[0]["snapshot_path"], "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["diff_id"], diff_id)

    def test_conflict_keep_preserves_current(self):
        """keep 策略下，冲突的操作被跳过，当前状态不变."""
        diff_id = self._setup_diff()
        plans_mod.save_plan(self.db_path, self.config, "plan-keep")
        pk = plans_mod.get_plan(self.db_path, self.config, "plan-keep")

        reviewer.set_remark(
            self.db_path, diff_id, "keep-日志时的旧备注", operator="k1",
            plan_id=pk["id"], plan_name=pk["name"],
        )
        reviewer.set_remark(self.db_path, diff_id, "当前最新备注", operator="k2")

        result = replay_mod.replay_operations(
            self.db_path, self.config["export"]["output_dir"],
            plan_id=pk["id"],
            default_conflict_resolution=replay_mod.CONFLICT_KEEP,
        )
        self.assertTrue(result["success"])
        self.assertGreaterEqual(len(result["conflicts"]), 1)
        current = db.get_difference(self.db_path, diff_id)
        self.assertEqual(current["remark"], "当前最新备注", "keep 策略不得覆盖当前状态")


class TestCliPlanCommands(_BaseTest):
    """CLI 新命令集成测试（plan-save/list/use/delete + set-operator + replay）."""

    def test_cli_plan_save_list_use_delete(self):
        """CLI 命令保存/列表/激活/删除方案闭环."""
        csv_path = self._make_batch("cli")
        self._import(csv_path)

        rc = self._cli(
            "plan-save", "cli-plan",
            "-s", "pending", "-f", "id,location,sku,status,remark",
            "-r", "cli 模板",
        )
        self.assertEqual(rc, 0)

        rc_list = self._cli("plan-list")
        self.assertEqual(rc_list, 0)

        rc_use = self._cli("plan-use", "cli-plan")
        self.assertEqual(rc_use, 0)
        reloaded = cfg.load_config(self.cfg_file)
        self.assertEqual(reloaded.get("active_plan"), "cli-plan")

        rc_del = self._cli("plan-delete", "cli-plan")
        self.assertEqual(rc_del, 0)
        reloaded2 = cfg.load_config(self.cfg_file)
        self.assertIsNone(reloaded2.get("active_plan"))

    def test_cli_set_operator_persists(self):
        """CLI set-operator 落盘 runtime_state."""
        rc = self._cli("set-operator", "charlie")
        self.assertEqual(rc, 0)
        reloaded = cfg.load_config(self.cfg_file)
        self.assertEqual(reloaded.get("operator"), "charlie")

    def test_cli_replay_command(self):
        """CLI replay 命令回放 status_change 还原状态."""
        diff_id = self._setup_diff()
        plans_mod.save_plan(self.db_path, self.config, "cli-rp")
        rp = plans_mod.get_plan(self.db_path, self.config, "cli-rp")
        reviewer.set_status(
            self.db_path, diff_id, "confirmed", operator="cli",
            plan_id=rp["id"], plan_name=rp["name"],
            allowed_statuses=self.config["status"]["allowed"],
        )
        reviewer.undo_last(self.db_path, operator="cli",
                           plan_id=rp["id"], plan_name=rp["name"])
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "pending")

        rc = self._cli("replay", "-p", "cli-rp", "-r", "abort",
                       "-a", "status_change")
        self.assertEqual(rc, 0)
        self.assertEqual(db.get_difference(self.db_path, diff_id)["status"], "confirmed")


class TestExportConsistency(_BaseTest):
    """导出一致性：文件名含方案元数据，表头记录方案与操作人."""

    def test_export_includes_plan_metadata(self):
        """差异导出文件名带 planID，CSV 首行记录方案/操作人/字段."""
        csv_path = self._make_batch("ex")
        self._import(csv_path)
        plans_mod.save_plan(
            self.db_path, self.config, "ex-plan",
            export_fields=["id", "location", "sku", "total_diff_qty", "status"],
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "ex-plan")
        cfg.set_operator(self.config, "dave")

        result = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            plan=plan, operator="dave",
        )
        self.assertTrue(result["success"])
        self.assertIn(f"_plan{plan['id']}", result["filename"])

        with open(result["file_path"], "r", encoding="utf-8-sig") as f:
            first_line = f.readline()
        self.assertIn("ex-plan", first_line)
        self.assertIn("dave", first_line)
        self.assertIn("id,location,sku", first_line)

    def test_export_summary_and_sources_log_to_op_logs(self):
        """summary / sources 导出均在 operation_logs 留痕."""
        csv_path = self._make_batch("s")
        self._import(csv_path)
        exporter.export_summary(self.db_path, self.config["export"]["output_dir"], operator="op")
        exporter.export_source_lines(self.db_path, self.config["export"]["output_dir"], operator="op")
        logs = db.get_operation_logs(self.db_path, action_type="export")
        types = {l["action_data"].get("export_type") for l in logs}
        self.assertIn("summary", types)
        self.assertIn("sources", types)


class TestPlanFilterExportConsistency(_BaseTest):
    """方案筛选在 list 和 export 链路必须一致（防 regression: 只 list 生效 export 漏了 location/sku）."""

    def test_list_and_export_have_same_count_for_location_filter(self):
        """方案只看 A- 库位时，list 和 export 的差异条数必须一致."""
        csv_path = self._make_batch("loc")
        self._import(csv_path)
        all_diffs = merger.get_merged_differences(self.db_path)
        self.assertEqual(len(all_diffs), 2)

        plans_mod.save_plan(
            self.db_path, self.config, "only-a",
            filter_location="A-",
            export_fields=["id", "location", "sku", "total_diff_qty"],
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "only-a")

        filtered_list = merger.get_merged_differences(
            self.db_path, location=plan["filter_location"],
        )
        self.assertEqual(len(filtered_list), 1)

        result = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            location=plan["filter_location"],
            plan=plan, operator="tester",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1, "导出条数必须和 list 筛选结果一致")

    def test_sku_filter_in_export_matches_list(self):
        """方案按 SKU 筛选时，export 条数必须和 list 一致."""
        csv_path = self._make_batch("sku")
        self._import(csv_path)

        plans_mod.save_plan(
            self.db_path, self.config, "only-sku-a",
            filter_sku="SKU_A",
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "only-sku-a")

        list_count = len(merger.get_merged_differences(
            self.db_path, sku=plan["filter_sku"],
        ))
        result = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            sku=plan["filter_sku"], plan=plan,
        )
        self.assertEqual(result["count"], list_count)

    def test_status_and_location_combined_filter_export(self):
        """状态 + 库位组合筛选，export 必须和 list 一致."""
        csv_path = self._make_batch("combo")
        self._import(csv_path)

        plans_mod.save_plan(
            self.db_path, self.config, "a-pending",
            filter_status="pending",
            filter_location="A-",
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "a-pending")

        list_count = len(merger.get_merged_differences(
            self.db_path, status="pending", location="A-",
        ))
        result = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            status="pending", location="A-", plan=plan,
        )
        self.assertEqual(result["count"], list_count)

    def test_cli_export_respects_plan_filter(self):
        """CLI export 命令必须应用当前方案的 location/sku 筛选."""
        csv_path = self._make_batch("cli-f")
        self._import(csv_path)

        self._cli("plan-save", "cli-only-b", "-l", "B-")
        self._cli("plan-use", "cli-only-b")

        all_result = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
        )
        self.assertEqual(all_result["count"], 2)

        rc = self._cli("export", "-t", "differences")
        self.assertEqual(rc, 0)

        export_dir = self.config["export"]["output_dir"]
        files = [f for f in os.listdir(export_dir) if f.startswith("audit_report_")]
        plan_files = [f for f in files if "_plan" in f]
        self.assertTrue(plan_files, "方案激活后导出文件必须带 planID")

        plan_file = max(plan_files, key=lambda f: os.path.getmtime(os.path.join(export_dir, f)))
        with open(os.path.join(export_dir, plan_file), "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        data_lines = [l for l in lines if not l.startswith("#") and l.strip()]
        self.assertEqual(len(data_lines) - 1, 1, "按 B- 库位筛选后应只有 1 条差异数据")


class TestExportReplayConsistency(_BaseTest):
    """导出回放一致性：回放出来的导出文件必须和原导出在方案标记、字段、条数、命名上一致."""

    def test_replay_export_preserves_plan_and_operator_in_filename(self):
        """回放 differences 导出，文件名必须带 planID 和原操作人一致."""
        csv_path = self._make_batch("rep")
        self._import(csv_path)

        plans_mod.save_plan(
            self.db_path, self.config, "replay-plan",
            filter_location="A-",
            export_fields=["id", "location", "sku", "status"],
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "replay-plan")

        orig = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            location="A-", plan=plan, operator="orig-user",
        )
        self.assertTrue(orig["success"])
        self.assertEqual(orig["count"], 1)
        self.assertIn(f"_plan{plan['id']}", orig["filename"])

        export_logs = db.get_operation_logs(self.db_path, action_type="export")
        self.assertEqual(len(export_logs), 1)
        log = export_logs[0]
        self.assertEqual(log["plan_name"], "replay-plan")
        self.assertEqual(log["operator"], "orig-user")
        self.assertEqual(log["action_data"]["location_filter"], "A-")
        self.assertEqual(
            log["action_data"]["export_fields"],
            ["id", "location", "sku", "status"],
        )

        os.remove(orig["file_path"])

        replay_dir = os.path.join(self.tmpdir, "replay_exports")
        result = replay_mod.replay_operations(
            self.db_path, replay_dir,
            plan_name="replay-plan",
            action_types=["export"],
            config_for_plan_lookup=self.config,
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(result["exports"]), 1)

        replayed = result["exports"][0]
        self.assertIn(f"_plan{plan['id']}", replayed["filename"])
        self.assertEqual(replayed["count"], 1)
        self.assertEqual(replayed["plan_name"], "replay-plan")

        with open(replayed["file_path"], "r", encoding="utf-8-sig") as f:
            header = f.readline()
        self.assertIn("replay-plan", header)
        self.assertIn("orig-user", header)
        self.assertIn("A-", header)
        self.assertIn("id,location,sku,status", header)

    def test_replay_export_respects_location_and_sku_filters(self):
        """回放导出必须应用原日志里的 location/sku 筛选，条数和原导出一致."""
        csv_path = os.path.join(self.tmpdir, "big.csv")
        _write_csv(
            csv_path,
            ["location", "sku", "expected_qty", "counted_qty"],
            [
                ["A-01", "X1", "100", "90"],
                ["A-02", "X2", "50", "45"],
                ["B-01", "X1", "200", "190"],
                ["B-02", "Y1", "30", "28"],
            ],
        )
        self._import(csv_path)
        self.assertEqual(len(merger.get_merged_differences(self.db_path)), 4)

        plans_mod.save_plan(
            self.db_path, self.config, "b-only",
            filter_location="B-", filter_sku="X",
        )
        plan = plans_mod.get_plan(self.db_path, self.config, "b-only")

        orig = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            location="B-", sku="X", plan=plan, operator="t1",
        )
        self.assertEqual(orig["count"], 1)

        replay_dir = os.path.join(self.tmpdir, "rep2")
        result = replay_mod.replay_operations(
            self.db_path, replay_dir,
            plan_id=plan["id"],
            action_types=["export"],
            config_for_plan_lookup=self.config,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["exports"][0]["count"], 1,
                         "回放导出必须和原导出条数一致（同样的 location+sku 筛选）")

    def test_restart_then_export_still_filtered(self):
        """重启（重新 load_config）后，激活方案的导出筛选仍然生效."""
        csv_path = self._make_batch("restart")
        self._import(csv_path)

        plans_mod.save_plan(
            self.db_path, self.config, "restart-plan",
            filter_location="B-",
            export_fields=["id", "sku", "total_diff_qty"],
        )
        cfg.set_active_plan(self.config, "restart-plan")
        cfg.set_operator(self.config, "restart-user")

        first_export = exporter.export_differences(
            self.db_path, self.config["export"]["output_dir"],
            location="B-",
            plan=plans_mod.get_plan(self.db_path, self.config, "restart-plan"),
            operator="restart-user",
        )
        self.assertEqual(first_export["count"], 1)

        reloaded = cfg.load_config(self.cfg_file)
        self.assertEqual(reloaded["active_plan"], "restart-plan")
        self.assertEqual(reloaded["operator"], "restart-user")

        restarted_plan = plans_mod.get_plan(
            self.db_path, reloaded, "restart-plan",
        )
        self.assertIsNotNone(restarted_plan)
        self.assertEqual(restarted_plan["filter_location"], "B-")

        second_export = exporter.export_differences(
            self.db_path, reloaded["export"]["output_dir"],
            location=restarted_plan["filter_location"],
            plan=restarted_plan,
            operator=reloaded["operator"],
        )
        self.assertEqual(second_export["count"], 1)
        self.assertEqual(second_export["plan_name"], "restart-plan")
        self.assertIn(f"_plan{restarted_plan['id']}", second_export["filename"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
