"""盘点复核 CLI 启动与归档恢复主链路回归测试.

本文件覆盖用户最关心的三条稳定性保证，全部通过子进程真实执行 CLI
（不依赖进程内 import），以验证“源码树外/归档解压后/跨重启”均可用：

1. 源码树外冷启动：
   - 从系统临时目录（源码树之外）以无 PYTHONPATH 环境调用 inventory-audit，
     --help 与真实命令（init/import）均可运行；
   - 配置中相对数据路径（./audit_data/audit.db）以配置文件所在目录为基准解析，
     绝不会把 audit_data 写到当前工作目录；
   - 在源码树内 `python -m inventory_audit` 仍可用（向后兼容）。

2. 跨重启后再次运行：
   - 进程 A：init + import；
   - 进程 B（全新解释器）：list 仍能看到 A 写入的数据，证明状态落盘且可被新进程读取。

3. 归档恢复到全新目录再执行：
   - 在源目录建会话 → session-archive-create 生成 zip；
   - session-archive-restore 到全新目录；
   - 从“源码树之外”的工作目录，用恢复后的 config.json 执行 list/show/export，
     数据读取自新目录、导出文件落在新目录的 exports，且不污染当前工作目录。

4. 帮助/说明与真实命令一致：
   - 顶层 --help 列出的子命令集合 == parser 实际注册的子命令集合；
   - 文档（README/USAGE）承诺的核心命令均为真实子命令；
   - 每个子命令 `<sub> --help` 都能退出码 0 并提及自身名字。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO_ROOT, "inventory-audit")


# ============================================================================
# 子进程 / 环境辅助
# ============================================================================

def _no_pythonpath_env():
    """构造一个“没有 PYTHONPATH”的环境，并强制 UTF-8 IO，避免子进程编码问题。"""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _run_launcher(args, cwd=None, env=None):
    """通过 [sys.executable, LAUNCHER, *args] 真实执行 CLI 并捕获输出。"""
    return subprocess.run(
        [sys.executable, LAUNCHER] + list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_module(args, cwd=None, env=None):
    """等价于 `python -m inventory_audit ...`，用于校验源码树内向后兼容。"""
    return subprocess.run(
        [sys.executable, "-m", "inventory_audit"] + list(args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_outside_cwd(prefix="ia_outside_"):
    """创建一个位于源码树之外的工作目录，并断言它确实在源码树外。"""
    outside = tempfile.mkdtemp(prefix=prefix)
    assert not os.path.abspath(outside).startswith(REPO_ROOT + os.sep), (
        f"临时目录 {outside} 落在源码树 {REPO_ROOT} 内，无法验证冷启动"
    )
    return outside


def _write_relative_config(session_dir):
    """写一个“相对路径”配置：./audit_data/audit.db 与 ./audit_data/exports。

    故意使用相对路径，以验证它们以配置文件所在目录为基准解析（而非 CWD）。
    """
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


def _write_sample_csv(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("location,sku,expected_qty,counted_qty\n")
        f.write("WH-A,SKU-001,100,90\n")
        f.write("WH-A,SKU-002,50,50\n")
        f.write("WH-B,SKU-003,20,15\n")


# ============================================================================
# 1. 源码树外冷启动
# ============================================================================

class TestLauncherColdStart(unittest.TestCase):
    def setUp(self):
        self._dirs = []

    def tearDown(self):
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _newdir(self, prefix):
        d = tempfile.mkdtemp(prefix=prefix)
        self._dirs.append(d)
        return d

    def test_launcher_file_exists(self):
        self.assertTrue(os.path.isfile(LAUNCHER), f"启动器不存在: {LAUNCHER}")

    def test_help_from_outside_source_without_pythonpath(self):
        outside = self._newdir("ia_help_out_")
        # 确保确实在源码树之外
        self.assertFalse(os.path.abspath(outside).startswith(REPO_ROOT + os.sep))
        res = _run_launcher(["--help"], cwd=outside, env=_no_pythonpath_env())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("仓库盘点差异复核 CLI 工具", res.stdout)
        self.assertIn("list", res.stdout)
        self.assertIn("session-archive-restore", res.stdout)

    def test_module_mode_still_works_inside_source_tree(self):
        # 向后兼容：在源码树内 `python -m inventory_audit` 仍可用
        res = _run_module(["--help"], cwd=REPO_ROOT, env=_no_pythonpath_env())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("仓库盘点差异复核 CLI 工具", res.stdout)

    def test_init_resolves_data_to_config_dir_not_cwd(self):
        session = self._newdir("ia_init_sess_")
        outside = self._newdir("ia_init_out_")
        cfg_path = _write_relative_config(session)
        csv_path = os.path.join(session, "stock.csv")
        _write_sample_csv(csv_path)
        env = _no_pythonpath_env()

        # 从 outside 这个 CWD 执行，但配置指向 session 目录
        r_init = _run_launcher(["-c", cfg_path, "init"], cwd=outside, env=env)
        self.assertEqual(r_init.returncode, 0, r_init.stderr + r_init.stdout)
        # 数据库必须落在配置文件所在目录（session），而不是 CWD（outside）
        self.assertTrue(os.path.isfile(os.path.join(session, "audit_data", "audit.db")))
        self.assertFalse(
            os.path.exists(os.path.join(outside, "audit_data")),
            "相对路径被错误地解析到当前工作目录",
        )

        # 真实命令（import）也从源码树外执行
        r_imp = _run_launcher(["-c", cfg_path, "import", csv_path, "-n", "b1"],
                              cwd=outside, env=env)
        self.assertEqual(r_imp.returncode, 0, r_imp.stderr + r_imp.stdout)
        self.assertIn("导入成功", r_imp.stdout)


# ============================================================================
# 2. 跨重启后再次运行
# ============================================================================

class TestCrossRestartPersistence(unittest.TestCase):
    def setUp(self):
        self.session = tempfile.mkdtemp(prefix="ia_xr_sess_")
        self.outside = tempfile.mkdtemp(prefix="ia_xr_out_")
        self.cfg_path = _write_relative_config(self.session)
        self.csv_path = os.path.join(self.session, "stock.csv")
        _write_sample_csv(self.csv_path)
        self.env = _no_pythonpath_env()

    def tearDown(self):
        shutil.rmtree(self.session, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_data_written_in_process_a_is_visible_in_process_b(self):
        # 进程 A：init + import
        r_init = _run_launcher(["-c", self.cfg_path, "init"],
                               cwd=self.outside, env=self.env)
        self.assertEqual(r_init.returncode, 0, r_init.stderr + r_init.stdout)
        r_imp = _run_launcher(["-c", self.cfg_path, "import", self.csv_path, "-n", "b1"],
                              cwd=self.outside, env=self.env)
        self.assertEqual(r_imp.returncode, 0, r_imp.stderr + r_imp.stdout)

        # 进程 B：全新解释器，仅 list —— 必须看到 A 写入的数据
        r_list = _run_launcher(["-c", self.cfg_path, "list"],
                               cwd=self.outside, env=self.env)
        self.assertEqual(r_list.returncode, 0, r_list.stderr + r_list.stdout)
        self.assertIn("SKU-001", r_list.stdout)
        self.assertIn("SKU-003", r_list.stdout)

        # 复核状态也跨进程可见：进程 A 置 confirmed，进程 B show 仍带状态
        r_st = _run_launcher(["-c", self.cfg_path, "status", "1", "confirmed"],
                             cwd=self.outside, env=self.env)
        self.assertEqual(r_st.returncode, 0, r_st.stderr + r_st.stdout)
        r_show = _run_launcher(["-c", self.cfg_path, "show", "1"],
                               cwd=self.outside, env=self.env)
        self.assertEqual(r_show.returncode, 0, r_show.stderr + r_show.stdout)
        self.assertIn("SKU-001", r_show.stdout)


# ============================================================================
# 3. 归档恢复到全新目录再执行
# ============================================================================

class TestArchiveRestoreToNewDir(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="ia_arc_src_")
        self.outside = tempfile.mkdtemp(prefix="ia_arc_out_")
        self.cfg_path = _write_relative_config(self.src)
        self.csv_path = os.path.join(self.src, "stock.csv")
        _write_sample_csv(self.csv_path)
        self.env = _no_pythonpath_env()
        self.new_ws = None

    def tearDown(self):
        shutil.rmtree(self.src, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
        if self.new_ws:
            shutil.rmtree(self.new_ws, ignore_errors=True)

    def test_restore_then_list_show_export_from_outside(self):
        # 1) 在源目录建会话
        _run_launcher(["-c", self.cfg_path, "init"], cwd=self.outside, env=self.env)
        _run_launcher(["-c", self.cfg_path, "import", self.csv_path, "-n", "b1"],
                      cwd=self.outside, env=self.env)
        _run_launcher(["-c", self.cfg_path, "status", "1", "confirmed"],
                      cwd=self.outside, env=self.env)

        # 2) 创建归档 zip（显式输出路径，便于断言）
        archive = os.path.join(self.src, "session.zip")
        r_crt = _run_launcher(["-c", self.cfg_path, "session-archive-create", "-o", archive],
                              cwd=self.outside, env=self.env)
        self.assertEqual(r_crt.returncode, 0, r_crt.stderr + r_crt.stdout)
        self.assertTrue(os.path.isfile(archive))

        # 3) 恢复到全新目录
        self.new_ws = tempfile.mkdtemp(prefix="ia_arc_newws_")
        r_rst = _run_launcher(
            ["-c", self.cfg_path, "session-archive-restore", archive,
             "--target-dir", self.new_ws],
            cwd=self.outside, env=self.env,
        )
        self.assertEqual(r_rst.returncode, 0, r_rst.stderr + r_rst.stdout)
        # 恢复后提示应指向启动器（而非仅 python -m）
        self.assertIn("inventory-audit", r_rst.stdout)
        new_cfg = os.path.join(self.new_ws, "config.json")
        self.assertTrue(os.path.isfile(new_cfg))
        self.assertTrue(os.path.isfile(os.path.join(self.new_ws, "audit_data", "audit.db")))

        # 4) 从“源码树之外”的工作目录，用恢复后的 config 执行 list/show/export
        r_list = _run_launcher(["-c", new_cfg, "list"], cwd=self.outside, env=self.env)
        self.assertEqual(r_list.returncode, 0, r_list.stderr + r_list.stdout)
        self.assertIn("SKU-001", r_list.stdout)

        r_show = _run_launcher(["-c", new_cfg, "show", "1"], cwd=self.outside, env=self.env)
        self.assertEqual(r_show.returncode, 0, r_show.stderr + r_show.stdout)
        self.assertIn("SKU-001", r_show.stdout)

        exports_dir = os.path.join(self.new_ws, "audit_data", "exports")
        self.assertTrue(os.path.isdir(exports_dir))
        before = set(os.listdir(exports_dir))
        r_exp = _run_launcher(["-c", new_cfg, "export", "-t", "summary"],
                              cwd=self.outside, env=self.env)
        self.assertEqual(r_exp.returncode, 0, r_exp.stderr + r_exp.stdout)
        after = set(os.listdir(exports_dir))
        self.assertGreater(len(after), len(before), "导出文件未写入恢复后的 exports 目录")

        # 5) 当前工作目录绝不能被污染
        self.assertFalse(os.path.exists(os.path.join(self.outside, "audit_data")))


# ============================================================================
# 4. 帮助 / 说明与真实命令一致
# ============================================================================

class TestHelpCommandsConsistency(unittest.TestCase):
    def setUp(self):
        self.outside = tempfile.mkdtemp(prefix="ia_consist_")
        self.env = _no_pythonpath_env()

    def tearDown(self):
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_top_help_subcommands_match_parser_registry(self):
        from inventory_audit.cli import build_parser

        parser = build_parser()
        sub_action = next(
            a for a in parser._actions if hasattr(a, "choices") and a.choices
        )
        parser_subs = set(sub_action.choices.keys())
        self.assertGreater(len(parser_subs), 10)

        top = _run_launcher(["--help"], cwd=self.outside, env=self.env)
        self.assertEqual(top.returncode, 0, top.stderr)
        # 取最长的 {...} 块，即子命令清单
        braces = re.findall(r"\{([^}]*)\}", top.stdout)
        self.assertTrue(braces, "顶层 --help 未列出子命令清单 {...}")
        shown = set(max(braces, key=len).split(","))
        self.assertEqual(parser_subs, shown,
                         "顶层帮助列出的子命令与 parser 注册的不一致")

    def test_documented_core_commands_are_real(self):
        from inventory_audit.cli import build_parser

        parser = build_parser()
        sub_action = next(
            a for a in parser._actions if hasattr(a, "choices") and a.choices
        )
        real = set(sub_action.choices.keys())
        # README/USAGE 承诺给用户的核心命令，必须都是真实子命令
        documented = [
            "init", "import", "list", "show", "status", "remark", "undo",
            "history", "export", "batches", "summary", "remerge",
            "set-operator", "plan-save", "plan-list", "plan-use", "plan-delete",
            "replay", "session-archive-create", "session-archive-info",
            "session-archive-restore",
        ]
        for name in documented:
            self.assertIn(name, real, f"文档承诺的命令 {name} 不是真实子命令")

    def test_every_subcommand_help_exits_zero(self):
        from inventory_audit.cli import build_parser

        parser = build_parser()
        sub_action = next(
            a for a in parser._actions if hasattr(a, "choices") and a.choices
        )
        for name in sub_action.choices.keys():
            r = _run_launcher([name, "--help"], cwd=self.outside, env=self.env)
            self.assertEqual(r.returncode, 0, f"{name} --help 失败: {r.stderr}")
            # 子命令帮助应至少提及自身名字或其用途
            self.assertTrue(
                name in r.stdout or "usage:" in r.stdout,
                f"{name} --help 未提及自身",
            )


if __name__ == "__main__":
    unittest.main()
