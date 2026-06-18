"""端到端回归测试 — 启动主链路 & 归档恢复后可运行路径.

两条最容易回归的链路：

1. 源码树内 ``python -m inventory_audit`` 主链路：
   init → import → list / show / export 完整走通，输出可断言。

2. 归档恢复后"文档承诺还能继续走"的启动方式：
   - session-archive-create 生成 zip
   - session-archive-restore 到新目录
   - 通过启动器（inventory-audit）在新目录外执行 list / show / export，结果与归档前一致
   - README 文档示例 ``cd + python -m`` 在源码树外不设 PYTHONPATH 时不可运行
   - CLI 恢复提示与真实可运行路径一致

设计原则：
- 全部用子进程真实跑命令（不依赖进程内 import），验证冷启动 / 跨重启 / 源码树外
- 复用现有夹具模式（相对路径 config + 样例 CSV + 临时目录）
- 不把 HTTP 方法词当命令执行（import 等子命令以列表参数传递，不经 shell）
- 不改业务逻辑；若文档示例与真实行为不一致，断言记录差异
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_ROOT, "inventory-audit")


def _env_no_pythonpath():
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run_module(args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, "-m", "inventory_audit"] + list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_launcher(args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, LAUNCHER] + list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_config(session_dir):
    config = {
        "database": {"path": "./audit_data/audit.db"},
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
        "export": {"output_dir": "./audit_data/exports"},
        "active_plan": None,
        "operator": "tester",
    }
    cfg_path = os.path.join(session_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)
    return cfg_path


def _make_csv(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("location,sku,expected_qty,counted_qty\n")
        f.write("WH-A,SKU-001,100,90\n")
        f.write("WH-A,SKU-002,50,50\n")
        f.write("WH-B,SKU-003,20,15\n")


class TestModuleModeMainChain(unittest.TestCase):
    """源码树内 python -m inventory_audit init→import→list 主链路."""

    def setUp(self):
        self.session = tempfile.mkdtemp(prefix="ia_e2e_mod_")
        self.cfg_path = _make_config(self.session)
        self.csv_path = os.path.join(self.session, "stock.csv")
        _make_csv(self.csv_path)
        self.env = _env_no_pythonpath()

    def tearDown(self):
        shutil.rmtree(self.session, ignore_errors=True)

    def test_init_import_list_chain(self):
        r = _run_module(["-c", self.cfg_path, "init"], cwd=REPO_ROOT, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("初始化完成", r.stdout)
        self.assertTrue(
            os.path.isfile(os.path.join(self.session, "audit_data", "audit.db"))
        )

        r = _run_module(
            ["-c", self.cfg_path, "import", self.csv_path, "-n", "e2e-batch"],
            cwd=REPO_ROOT,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("导入成功", r.stdout)
        self.assertIn("e2e-batch", r.stdout)

        r = _run_module(["-c", self.cfg_path, "list"], cwd=REPO_ROOT, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("SKU-001", r.stdout)
        self.assertIn("SKU-003", r.stdout)
        self.assertNotIn("SKU-002", r.stdout)

    def test_show_after_import(self):
        _run_module(["-c", self.cfg_path, "init"], cwd=REPO_ROOT, env=self.env)
        _run_module(
            ["-c", self.cfg_path, "import", self.csv_path, "-n", "e2e-batch"],
            cwd=REPO_ROOT,
            env=self.env,
        )

        r = _run_module(
            ["-c", self.cfg_path, "show", "1"], cwd=REPO_ROOT, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("差异详情", r.stdout)
        self.assertIn("SKU-001", r.stdout)
        self.assertIn("WH-A", r.stdout)

    def test_export_after_import(self):
        _run_module(["-c", self.cfg_path, "init"], cwd=REPO_ROOT, env=self.env)
        _run_module(
            ["-c", self.cfg_path, "import", self.csv_path, "-n", "e2e-batch"],
            cwd=REPO_ROOT,
            env=self.env,
        )

        r = _run_module(
            ["-c", self.cfg_path, "export", "-t", "differences"],
            cwd=REPO_ROOT,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("导出成功", r.stdout)

        exports_dir = os.path.join(self.session, "audit_data", "exports")
        csvs = [f for f in os.listdir(exports_dir) if f.endswith(".csv")]
        self.assertGreater(len(csvs), 0)
        exported = os.path.join(exports_dir, csvs[0])
        with open(exported, "r", encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("SKU-001", content)


class TestArchiveRestoreRunnablePath(unittest.TestCase):
    """归档恢复后文档承诺还能继续走的启动方式 — 逐条断言."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="ia_e2e_src_")
        self.outside = tempfile.mkdtemp(prefix="ia_e2e_out_")
        self.cfg_path = _make_config(self.src)
        self.csv_path = os.path.join(self.src, "stock.csv")
        _make_csv(self.csv_path)
        self.env = _env_no_pythonpath()
        self.new_ws = None

        _run_launcher(
            ["-c", self.cfg_path, "init"], cwd=self.outside, env=self.env
        )
        _run_launcher(
            ["-c", self.cfg_path, "import", self.csv_path, "-n", "e2e-batch"],
            cwd=self.outside,
            env=self.env,
        )
        _run_launcher(
            ["-c", self.cfg_path, "status", "1", "confirmed"],
            cwd=self.outside,
            env=self.env,
        )

        self.archive = os.path.join(self.src, "session.zip")
        r = _run_launcher(
            ["-c", self.cfg_path, "session-archive-create", "-o", self.archive],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

        self.new_ws = tempfile.mkdtemp(prefix="ia_e2e_newws_")
        self.restore_output = _run_launcher(
            ["-c", self.cfg_path, "session-archive-restore",
             self.archive, "--target-dir", self.new_ws],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(
            self.restore_output.returncode, 0,
            self.restore_output.stderr + self.restore_output.stdout,
        )
        self.new_cfg = os.path.join(self.new_ws, "config.json")

    def tearDown(self):
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
        if self.new_ws:
            shutil.rmtree(self.new_ws, ignore_errors=True)

    def test_restore_output_mentions_launcher(self):
        self.assertIn("inventory-audit", self.restore_output.stdout)

    def test_restore_output_suggests_python_launcher_not_only_m(self):
        lines = self.restore_output.stdout.splitlines()
        suggestion_lines = [l.strip() for l in lines if "list" in l and "python" in l]
        self.assertTrue(
            any("inventory-audit" in l for l in suggestion_lines),
            "恢复提示应包含启动器路径（python inventory-audit ...），"
            f"实际提示行: {suggestion_lines}",
        )

    def test_launcher_list_from_outside_cwd(self):
        r = _run_launcher(
            ["-c", self.new_cfg, "list"], cwd=self.outside, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("SKU-001", r.stdout)
        self.assertIn("SKU-003", r.stdout)

    def test_launcher_show_from_outside_cwd(self):
        r = _run_launcher(
            ["-c", self.new_cfg, "show", "1"], cwd=self.outside, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("差异详情", r.stdout)
        self.assertIn("SKU-001", r.stdout)
        self.assertIn("WH-A", r.stdout)

    def test_launcher_export_differences_from_outside_cwd(self):
        exports_dir = os.path.join(self.new_ws, "audit_data", "exports")
        before = set(os.listdir(exports_dir)) if os.path.isdir(exports_dir) else set()
        r = _run_launcher(
            ["-c", self.new_cfg, "export", "-t", "differences"],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("导出成功", r.stdout)
        after = set(os.listdir(exports_dir))
        new_files = after - before
        self.assertGreater(len(new_files), 0)
        exported = os.path.join(exports_dir, sorted(new_files)[0])
        with open(exported, "r", encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("SKU-001", content)
        self.assertIn("SKU-003", content)

    def test_launcher_export_summary_from_outside_cwd(self):
        exports_dir = os.path.join(self.new_ws, "audit_data", "exports")
        before = set(os.listdir(exports_dir)) if os.path.isdir(exports_dir) else set()
        r = _run_launcher(
            ["-c", self.new_cfg, "export", "-t", "summary"],
            cwd=self.outside,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("导出成功", r.stdout)
        after = set(os.listdir(exports_dir))
        self.assertGreater(len(after), len(before))

    def test_module_mode_from_source_tree_with_restored_config(self):
        r = _run_module(
            ["-c", self.new_cfg, "list"], cwd=REPO_ROOT, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("SKU-001", r.stdout)

    def test_module_mode_from_new_workspace_without_pythonpath_fails(self):
        r = _run_module(
            ["-c", "config.json", "list"], cwd=self.new_ws, env=self.env
        )
        self.assertNotEqual(
            r.returncode, 0,
            "python -m 在源码树外不应成功；"
            "若此断言失败说明环境变化导致文档示例可能已可运行，需同步更新断言",
        )
        self.assertIn("No module named", r.stderr)

    def test_list_output_format_has_expected_columns(self):
        r = _run_launcher(
            ["-c", self.new_cfg, "list"], cwd=self.outside, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        for col in ("ID", "库位", "SKU", "差异数", "状态"):
            self.assertIn(col, r.stdout, f"list 输出缺少列头: {col}")

    def test_show_output_format_has_expected_fields(self):
        r = _run_launcher(
            ["-c", self.new_cfg, "show", "1"], cwd=self.outside, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        for field in ("库位:", "SKU:", "差异数量:", "状态:"):
            self.assertIn(field, r.stdout, f"show 输出缺少字段: {field}")

    def test_export_file_written_to_restored_exports_dir_not_source(self):
        new_exports = os.path.join(self.new_ws, "audit_data", "exports")
        src_exports = os.path.join(self.src, "audit_data", "exports")
        before_new = (
            set(os.listdir(new_exports)) if os.path.isdir(new_exports) else set()
        )
        before_src = (
            set(os.listdir(src_exports)) if os.path.isdir(src_exports) else set()
        )

        _run_launcher(
            ["-c", self.new_cfg, "export", "-t", "summary"],
            cwd=self.outside,
            env=self.env,
        )

        after_new = set(os.listdir(new_exports))
        after_src = (
            set(os.listdir(src_exports)) if os.path.isdir(src_exports) else set()
        )
        self.assertGreater(len(after_new), len(before_new))
        self.assertEqual(len(after_src), len(before_src))

    def test_show_reflects_status_change_done_before_archive(self):
        r = _run_launcher(
            ["-c", self.new_cfg, "show", "1"], cwd=self.outside, env=self.env
        )
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("已确认", r.stdout)


class TestReadmeUsageCommandConsistency(unittest.TestCase):
    """README / USAGE 命令示例与真实可运行路径一致性.

    核心发现：README「审计会话归档与迁移」段落写的是::

        cd /path/to/new_workspace
        python -m inventory_audit -c config.json list

    但 ``python -m inventory_audit`` 在源码树外不设 PYTHONPATH 时
    不可运行（ModuleNotFoundError）。真正可用的方式是启动器::

        python <repo_root>/inventory-audit -c config.json list

    以下测试断言这一行为差异，防止文档承诺与真实行为不一致时无人发现。
    如未来安装机制变化使 ``python -m`` 在源码树外也可用，
    对应断言会失败，提示同步更新文档。
    """

    def setUp(self):
        self.env = _env_no_pythonpath()
        self.outside = tempfile.mkdtemp(prefix="ia_e2e_doc_")

    def tearDown(self):
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_readme_restore_example_python_m_fails_outside_tree(self):
        r = _run_module(
            ["-c", "config.json", "list"], cwd=self.outside, env=self.env
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No module named", r.stderr)

    def test_launcher_from_any_cwd_works(self):
        r = _run_launcher(["--help"], cwd=self.outside, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("仓库盘点差异复核 CLI 工具", r.stdout)

    def test_restore_cli_output_matches_actual_runnable_commands(self):
        src = tempfile.mkdtemp(prefix="ia_doc_src_")
        outside = tempfile.mkdtemp(prefix="ia_doc_out_")
        new_ws = None
        try:
            cfg_path = _make_config(src)
            csv_path = os.path.join(src, "stock.csv")
            _make_csv(csv_path)

            _run_launcher(["-c", cfg_path, "init"], cwd=outside, env=self.env)
            _run_launcher(
                ["-c", cfg_path, "import", csv_path, "-n", "doc-test"],
                cwd=outside,
                env=self.env,
            )
            archive = os.path.join(src, "session.zip")
            _run_launcher(
                ["-c", cfg_path, "session-archive-create", "-o", archive],
                cwd=outside,
                env=self.env,
            )

            new_ws = tempfile.mkdtemp(prefix="ia_doc_newws_")
            r = _run_launcher(
                ["-c", cfg_path, "session-archive-restore",
                 archive, "--target-dir", new_ws],
                cwd=outside,
                env=self.env,
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

            suggested_cmd = None
            for line in r.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("python") and "list" in stripped:
                    suggested_cmd = stripped
                    break
            self.assertIsNotNone(suggested_cmd, "恢复输出未找到 python ... list 命令提示")
            self.assertIn(
                "inventory-audit", suggested_cmd,
                "恢复提示应使用启动器（inventory-audit），而非仅 python -m",
            )

            parts = suggested_cmd.split()
            self.assertGreaterEqual(len(parts), 2, f"命令格式异常: {suggested_cmd}")
            executable = parts[1]
            self.assertTrue(
                os.path.isfile(executable) or executable == "inventory-audit",
                f"提示中的可执行路径不存在: {executable}",
            )

            new_cfg = os.path.join(new_ws, "config.json")
            test_args = ["-c", new_cfg, "list"]
            verify_r = _run_launcher(test_args, cwd=outside, env=self.env)
            self.assertEqual(
                verify_r.returncode, 0,
                f"恢复提示的命令不可运行: {suggested_cmd}\n"
                f"stderr: {verify_r.stderr}\nstdout: {verify_r.stdout}",
            )
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)
            if new_ws:
                shutil.rmtree(new_ws, ignore_errors=True)

    def test_usage_quickstart_init_command_works_from_source_tree(self):
        session = tempfile.mkdtemp(prefix="ia_doc_qstart_")
        try:
            cfg_path = _make_config(session)
            r = _run_module(
                ["-c", cfg_path, "init"], cwd=REPO_ROOT, env=self.env
            )
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn("初始化完成", r.stdout)
        finally:
            shutil.rmtree(session, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
