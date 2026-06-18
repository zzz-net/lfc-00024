"""审计会话归档包模块 - 把整段盘点会话打包为可搬走的归档，并支持恢复到新工作目录.

与 archive.py（单次模板执行的清单 JSON）不同，本模块打包的是**整段审计会话**：
盘点数据库、配置快照、导出报表、操作日志，统一成一个 zip 归档文件，
可以拷贝到另一台机器 / 另一个目录后用 restore 重建出可继续工作的环境。

归档内部结构（zip）：
- manifest.json            归档清单（schema / 版本 / 摘要 / 文件列表与 sha256）
- data/audit.db            盘点数据库快照（含批次/差异/方案/模板/执行记录/操作日志）
- data/runtime_state.json  active_plan + operator 运行时状态
- data/config.json         配置快照（剥离运行时状态的基础配置）
- data/exports/*.csv       导出报表
- data/plans/*.json        方案 JSON 双写
- data/templates/*.json   模板 JSON 双写
- data/batch_templates/*.json 批量任务模板 JSON 双写
- data/operation_logs.json 操作日志导出（便于人工核查；恢复时不回灌，DB 已含）

失败场景与错误码约定：
- 路径不存在 / 目录不可写 / 归档损坏 / 版本不兼容 均返回结构化 error，
  由 CLI 层翻译为带修正建议的提示，绝不静默覆盖已有数据库或同名配置。
- 关键动作（创建归档、恢复归档）写入 operation_logs。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import __version__ as TOOL_VERSION
from . import db


ARCHIVE_SCHEMA = "inventory_audit_session_archive"
ARCHIVE_VERSION = 1
SUPPORTED_ARCHIVE_VERSIONS: Tuple[int, ...] = (1,)

MANIFEST_NAME = "manifest.json"
DATA_PREFIX = "data/"

# 恢复时落盘会跳过的归档内文件（config 由恢复流程改写路径后另写，operation_logs 仅作留档）
SKIP_RESTORE_ENTRIES = frozenset({"data/config.json", "data/operation_logs.json"})

VALID_CONFLICT_STRATEGIES = ("abort", "rename", "overwrite")


# ---------------------------------------------------------------------------
# 路径 / 哈希 / 摘要 辅助
# ---------------------------------------------------------------------------

def _audit_data_dir(config: Dict[str, Any]) -> str:
    """审计数据目录（数据库所在目录，所有 JSON 双写子目录的父级）."""
    return os.path.dirname(os.path.abspath(config["database"]["path"]))


def _sha256_file(path: str) -> str:
    """计算文件 sha256（按块读取，适合大文件）."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _collect_dir_files(dir_path: str) -> List[Tuple[str, str]]:
    """收集目录下所有文件，返回 [(相对路径(正斜杠), 绝对路径)]；目录不存在返回空."""
    if not os.path.isdir(dir_path):
        return []
    result: List[Tuple[str, str]] = []
    for root, _dirs, files in os.walk(dir_path):
        for name in sorted(files):
            abspath = os.path.join(root, name)
            rel = os.path.relpath(abspath, dir_path).replace(os.sep, "/")
            result.append((rel, abspath))
    return result


def _db_row_counts(db_path: str) -> Dict[str, int]:
    """统计关键表的行数，用于归档摘要与恢复后核对；表缺失时记 0."""
    tables = [
        "batches", "source_lines", "differences", "review_history",
        "plans", "operation_logs", "templates", "batch_task_templates",
        "template_executions",
    ]
    counts: Dict[str, int] = {}
    if not os.path.exists(db_path):
        return {t: 0 for t in tables}
    try:
        with sqlite3.connect(db_path) as conn:
            for t in tables:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
                    counts[t] = int(row[0]) if row else 0
                except sqlite3.Error:
                    counts[t] = 0
    except sqlite3.Error:
        return {t: 0 for t in tables}
    return counts


def _config_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """生成基础配置快照（剥离 active_plan / operator 运行时状态）.

    运行时状态单独由 runtime_state.json 承载，避免恢复时与配置文件双写冲突。
    """
    snapshot = json.loads(json.dumps(config, ensure_ascii=False))
    snapshot.pop("active_plan", None)
    snapshot.pop("operator", None)
    return snapshot


def _snapshot_database(db_path: str) -> str:
    """用 SQLite backup API 导出一份一致性快照到临时文件，返回临时路径.

    相比直接拷贝 .db 文件，backup 能避免 journal/WAL 侧car导致的半写状态，
    保证归档里的数据库是自洽可打开的。调用方负责删除临时文件。
    """
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="session_snap_")
    os.close(fd)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return tmp


def _default_archive_path(config: Dict[str, Any], operator: str) -> str:
    """默认归档输出路径：<audit_data>/archives/session_<operator>_<时间戳>.zip."""
    archives_dir = os.path.join(_audit_data_dir(config), "archives")
    os.makedirs(archives_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_op = "".join(c if c.isalnum() or c in "-_" else "_" for c in (operator or "cli"))
    return os.path.join(archives_dir, f"session_{safe_op}_{ts}.zip")


# ---------------------------------------------------------------------------
# 创建归档
# ---------------------------------------------------------------------------

def create_session_archive(
    db_path: str,
    config: Dict[str, Any],
    config_file_path: Optional[str] = None,
    output_path: Optional[str] = None,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """把当前审计会话打包成可搬走的 zip 归档.

    Args:
        db_path: 盘点数据库路径
        config: 当前生效配置
        config_file_path: 触发命令时的 -c 配置文件路径（仅记录到清单，便于溯源）
        output_path: 自定义输出路径；None 时自动生成文件名
        operator: 操作人，None 时取 config.operator

    Returns:
        {"success", "archive_path", "manifest", "summary"} 或 {"success": False, "error"}
    """
    if not os.path.exists(db_path):
        return {"success": False, "error": f"数据库文件不存在：{db_path}。请先运行 init 并导入数据后再归档。"}
    if not os.path.isfile(db_path):
        return {"success": False, "error": f"数据库路径不是文件：{db_path}"}

    operator = operator or config.get("operator", "cli")
    audit_dir = _audit_data_dir(config)
    export_dir = os.path.abspath(config["export"]["output_dir"])

    # 确定输出路径
    if output_path is None:
        output_path = _default_archive_path(config, operator)
    abs_output = os.path.abspath(output_path)
    parent = os.path.dirname(abs_output) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        return {"success": False, "error": f"无法创建输出目录 {parent}：{e}"}
    if os.path.isdir(abs_output):
        return {"success": False, "error": f"输出路径已存在且是目录（请指定文件路径）：{abs_output}"}
    if not os.access(parent, os.W_OK):
        return {"success": False, "error": f"输出目录不可写：{parent}。请检查权限或换一个可写路径。"}

    # 收集要打包的磁盘文件：(归档内路径, 磁盘绝对路径)
    disk_files: List[Tuple[str, str]] = []
    runtime_state_path = os.path.join(audit_dir, "runtime_state.json")
    if os.path.exists(runtime_state_path):
        disk_files.append(("data/runtime_state.json", runtime_state_path))
    for rel, abspath in _collect_dir_files(export_dir):
        disk_files.append((f"data/exports/{rel}", abspath))
    for sub in ("plans", "templates", "batch_templates"):
        sub_dir = os.path.join(audit_dir, sub)
        for rel, abspath in _collect_dir_files(sub_dir):
            disk_files.append((f"data/{sub}/{rel}", abspath))

    # 内存文件：config 快照 + operation_logs 导出 + db 一致性快照
    base_config = _config_snapshot(config)
    op_logs = db.get_operation_logs(db_path)
    config_bytes = json.dumps(base_config, ensure_ascii=False, indent=2).encode("utf-8")
    op_logs_bytes = json.dumps(op_logs, ensure_ascii=False, indent=2).encode("utf-8")

    db_snap_tmp: Optional[str] = None
    try:
        db_snap_tmp = _snapshot_database(db_path)
        db_size = os.path.getsize(db_snap_tmp)
        db_sha = _sha256_file(db_snap_tmp)

        # 构建文件清单（含磁盘文件与内存文件），用于 manifest 与完整性校验
        file_entries: List[Dict[str, Any]] = []
        file_entries.append({
            "archive_path": "data/audit.db", "source": "database_snapshot",
            "size_bytes": db_size, "sha256": db_sha,
        })
        for arc, src in disk_files:
            if not os.path.exists(src):
                continue
            file_entries.append({
                "archive_path": arc, "source": src,
                "size_bytes": os.path.getsize(src), "sha256": _sha256_file(src),
            })
        file_entries.append({
            "archive_path": "data/config.json", "source": "config_snapshot",
            "size_bytes": len(config_bytes), "sha256": _sha256_bytes(config_bytes),
        })
        file_entries.append({
            "archive_path": "data/operation_logs.json", "source": "operation_logs_export",
            "size_bytes": len(op_logs_bytes), "sha256": _sha256_bytes(op_logs_bytes),
        })

        exports_summary = [
            {"filename": os.path.basename(f["archive_path"]),
             "size_bytes": f["size_bytes"]}
            for f in file_entries if f["archive_path"].startswith("data/exports/")
        ]
        manifest = {
            "$schema": ARCHIVE_SCHEMA,
            "$archive_version": ARCHIVE_VERSION,
            "tool_version": TOOL_VERSION,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "operator": operator,
            "active_plan": config.get("active_plan"),
            "source": {
                "db_path": os.path.abspath(db_path),
                "config_file": os.path.abspath(config_file_path) if config_file_path else None,
                "audit_data_dir": audit_dir,
                "export_dir": export_dir,
            },
            "summary": {
                "database": {
                    "archive_path": "data/audit.db",
                    "size_bytes": db_size,
                    "row_counts": _db_row_counts(db_path),
                },
                "config": {"archive_path": "data/config.json"},
                "exports": exports_summary,
                "plans": [os.path.basename(f["archive_path"])
                          for f in file_entries if f["archive_path"].startswith("data/plans/")],
                "templates": [os.path.basename(f["archive_path"])
                              for f in file_entries if f["archive_path"].startswith("data/templates/")],
                "batch_templates": [os.path.basename(f["archive_path"])
                                   for f in file_entries if f["archive_path"].startswith("data/batch_templates/")],
                "operation_logs_count": len(op_logs),
                "runtime_state_present": os.path.exists(runtime_state_path),
                "total_files": len(file_entries),
            },
            "files": file_entries,
        }

        # 写 zip
        try:
            with zipfile.ZipFile(abs_output, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_snap_tmp, "data/audit.db")
                for arc, src in disk_files:
                    if os.path.exists(src):
                        zf.write(src, arc)
                zf.writestr("data/config.json", config_bytes)
                zf.writestr("data/operation_logs.json", op_logs_bytes)
                zf.writestr(MANIFEST_NAME,
                            json.dumps(manifest, ensure_ascii=False, indent=2))
        except OSError as e:
            # 写失败时清理半成品文件，避免留下损坏归档
            if os.path.exists(abs_output):
                try:
                    os.remove(abs_output)
                except OSError:
                    pass
            return {"success": False, "error": f"写入归档文件失败：{e}。请检查目标路径是否可写或磁盘是否已满。"}
    finally:
        if db_snap_tmp and os.path.exists(db_snap_tmp):
            try:
                os.remove(db_snap_tmp)
            except OSError:
                pass

    # 关键动作写入日志（归档创建）
    try:
        db.restore_operation_log(
            db_path, plan_id=None, plan_name=None, operator=operator,
            action_type="session_archive",
            target_diff_id=None,
            action_data={
                "archive_path": abs_output,
                "output_path": output_path,
                "total_files": manifest["summary"]["total_files"],
                "db_size_bytes": manifest["summary"]["database"]["size_bytes"],
                "operation_logs_count": manifest["summary"]["operation_logs_count"],
            },
            snapshot_before=None,
        )
    except Exception:
        # 日志失败不应影响归档成功
        pass

    return {
        "success": True,
        "archive_path": abs_output,
        "manifest": manifest,
        "summary": manifest["summary"],
    }


# ---------------------------------------------------------------------------
# 读取 / 预览归档
# ---------------------------------------------------------------------------

def read_archive_manifest(archive_path: str) -> Dict[str, Any]:
    """加载并校验归档清单（不恢复），返回 manifest 与完整性信息."""
    if not os.path.exists(archive_path):
        return {"success": False, "error": f"归档文件不存在：{archive_path}。请检查路径是否正确。"}
    if os.path.isdir(archive_path):
        return {"success": False, "error": f"路径是目录而非归档文件：{archive_path}"}
    if not zipfile.is_zipfile(archive_path):
        return {"success": False, "error": f"归档文件损坏或不是有效 zip：{archive_path}。请确认这是 session-archive-create 产生的归档。"}
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = set(zf.namelist())
            if MANIFEST_NAME not in names:
                return {"success": False, "error": f"归档缺少 {MANIFEST_NAME}，可能已损坏或不是会话归档。"}
            try:
                manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                return {"success": False, "error": f"归档清单损坏（manifest JSON 解析失败）：{e}"}
            # 完整性校验：zip 自检 + 声明文件是否齐全
            bad = zf.testzip()
            if bad is not None:
                return {"success": False, "error": f"归档数据损坏，文件 {bad} 校验失败。"}
            if manifest.get("$schema") != ARCHIVE_SCHEMA:
                return {"success": False, "error": (
                    f"归档 schema 不匹配：{manifest.get('$schema')}（期望 {ARCHIVE_SCHEMA}）。"
                    f"这可能不是审计会话归档文件。"
                )}
            ver = manifest.get("$archive_version")
            if ver not in SUPPORTED_ARCHIVE_VERSIONS:
                supported = ", ".join(f"v{v}" for v in SUPPORTED_ARCHIVE_VERSIONS)
                return {
                    "success": False,
                    "error": (
                        f"归档版本不兼容：归档为 v{ver}，当前工具仅支持 {supported}。"
                        f"请用与该归档同版本的工具恢复，或升级本工具后再试。"
                    ),
                    "version": ver,
                    "incompatible": True,
                }
            if "data/audit.db" not in names:
                return {"success": False, "error": "归档缺少数据库文件 data/audit.db，无法恢复。"}
            if not manifest.get("files"):
                return {"success": False, "error": "归档清单缺少文件列表，可能已损坏。"}
            missing = [f["archive_path"] for f in manifest["files"]
                       if f["archive_path"] not in names]
            if missing:
                return {"success": False, "error": f"归档文件不完整，缺失：{', '.join(missing)}"}
    except zipfile.BadZipFile as e:
        return {"success": False, "error": f"归档文件损坏（无法打开 zip）：{e}"}
    except OSError as e:
        return {"success": False, "error": f"读取归档文件失败：{e}"}

    return {"success": True, "manifest": manifest, "archive_path": os.path.abspath(archive_path)}


def list_archive_contents(archive_path: str) -> Dict[str, Any]:
    """列出归档内容摘要（不恢复），供 session-archive-info 使用."""
    result = read_archive_manifest(archive_path)
    if not result.get("success"):
        return result
    manifest = result["manifest"]
    return {
        "success": True,
        "archive_path": result["archive_path"],
        "manifest": manifest,
        "summary": manifest.get("summary", {}),
        "files": manifest.get("files", []),
    }


# ---------------------------------------------------------------------------
# 恢复归档
# ---------------------------------------------------------------------------

def _base_restore_paths(target_root: str) -> Dict[str, str]:
    """恢复到 target_root 的基础（非重命名）目标路径."""
    audit_data_dir = os.path.join(target_root, "audit_data")
    return {
        "audit_data_dir": audit_data_dir,
        "db_path": os.path.join(audit_data_dir, "audit.db"),
        "export_dir": os.path.join(audit_data_dir, "exports"),
        "plans_dir": os.path.join(audit_data_dir, "plans"),
        "templates_dir": os.path.join(audit_data_dir, "templates"),
        "batch_templates_dir": os.path.join(audit_data_dir, "batch_templates"),
        "runtime_state_path": os.path.join(audit_data_dir, "runtime_state.json"),
        "config_path": os.path.join(target_root, "config.json"),
    }


def _unique_dir_path(target_root: str, base_name: str) -> str:
    """在 target_root 下找一个不存在的目录名：base_name, base_name_2, ..."""
    candidate = os.path.join(target_root, base_name)
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(target_root, f"{base_name}_{i}")
        i += 1
    return candidate


def _unique_file_path(target_root: str, base_name: str, ext: str) -> str:
    """在 target_root 下找一个不存在的文件名：base_name+ext, base_name_2+ext, ..."""
    candidate = os.path.join(target_root, f"{base_name}{ext}")
    i = 2
    while os.path.exists(candidate):
        candidate = os.path.join(target_root, f"{base_name}_{i}{ext}")
        i += 1
    return candidate


def detect_restore_conflicts(archive_path: str, target_root: str) -> List[Dict[str, Any]]:
    """检测恢复到 target_root（基础布局）的冲突，供预览/abort 判断使用."""
    paths = _base_restore_paths(target_root)
    conflicts: List[Dict[str, Any]] = []
    if os.path.exists(paths["db_path"]):
        conflicts.append({
            "type": "database_exists",
            "severity": "error",
            "message": f"目标数据库已存在：{paths['db_path']}",
            "target_path": paths["db_path"],
            "resolution": "使用 --conflict rename 另存到 audit_data_restored/，或 --overwrite 覆盖",
        })
    if os.path.exists(paths["config_path"]):
        conflicts.append({
            "type": "config_exists",
            "severity": "error",
            "message": f"同名配置已存在：{paths['config_path']}",
            "target_path": paths["config_path"],
            "resolution": "使用 --conflict rename 另存为 config_restored.json，或 --overwrite 覆盖",
        })
    return conflicts


def _resolve_target_paths(
    target_root: str, conflict: str, conflicts: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], bool]:
    """根据冲突策略确定最终落盘路径，返回 (paths, renamed)."""
    base = _base_restore_paths(target_root)
    has_conflict = bool(conflicts)

    if conflict == "overwrite":
        return base, False
    if conflict == "rename" and has_conflict:
        audit_data_dir = _unique_dir_path(target_root, "audit_data_restored")
        config_path = _unique_file_path(target_root, "config_restored", ".json")
        return {
            "audit_data_dir": audit_data_dir,
            "db_path": os.path.join(audit_data_dir, "audit.db"),
            "export_dir": os.path.join(audit_data_dir, "exports"),
            "plans_dir": os.path.join(audit_data_dir, "plans"),
            "templates_dir": os.path.join(audit_data_dir, "templates"),
            "batch_templates_dir": os.path.join(audit_data_dir, "batch_templates"),
            "runtime_state_path": os.path.join(audit_data_dir, "runtime_state.json"),
            "config_path": config_path,
        }, True
    # abort 或无冲突的 rename/overwrite：用基础布局
    return base, False


def restore_session_archive(
    archive_path: str,
    target_root: str,
    conflict: str = "abort",
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """从归档恢复到 target_root 工作目录，重建可继续工作的审计环境.

    Args:
        archive_path: 归档 zip 路径
        target_root: 恢复目标工作目录（不存在会自动创建）
        conflict: abort|rename|overwrite
            - abort   检测到已有数据库或同名配置即中止，不改动任何数据
            - rename  冲突时把恢复内容另存到 audit_data_restored/ 与 config_restored.json
            - overwrite 直接覆盖已有数据库与配置文件，导出文件按同名覆盖合并
        operator: 记录到恢复日志的操作人

    Returns:
        {"success", "target_root", "db_path", "config_path", "audit_data_dir",
         "renamed", "restored_files", "row_counts", "conflicts"} 或失败信息。
    """
    if conflict not in VALID_CONFLICT_STRATEGIES:
        return {"success": False, "error": f"未知冲突策略：{conflict}（允许 {', '.join(VALID_CONFLICT_STRATEGIES)}）"}

    read_result = read_archive_manifest(archive_path)
    if not read_result.get("success"):
        return read_result
    manifest = read_result["manifest"]

    # 目标目录可写性检查
    if os.path.exists(target_root) and not os.path.isdir(target_root):
        return {"success": False, "error": f"目标路径已存在且不是目录：{target_root}"}
    try:
        os.makedirs(target_root, exist_ok=True)
    except OSError as e:
        return {"success": False, "error": f"无法创建目标目录 {target_root}：{e}"}
    if not os.access(target_root, os.W_OK):
        return {"success": False, "error": f"目标目录不可写：{target_root}。请检查权限。"}

    conflicts = detect_restore_conflicts(archive_path, target_root)
    if conflicts and conflict == "abort":
        return {
            "success": False, "conflict": True, "conflicts": conflicts,
            "error": (
                "检测到已有数据库或同名配置，恢复中止（未改动任何数据）。"
                "使用 --conflict rename 另存，或 --conflict overwrite 覆盖后再恢复。"
            ),
        }

    paths, renamed = _resolve_target_paths(target_root, conflict, conflicts)
    audit_data_dir = paths["audit_data_dir"]
    db_path = paths["db_path"]
    config_path = paths["config_path"]

    # 创建必要的子目录
    for key in ("export_dir", "plans_dir", "templates_dir", "batch_templates_dir"):
        try:
            os.makedirs(paths[key], exist_ok=True)
        except OSError as e:
            return {"success": False, "error": f"创建目录 {paths[key]} 失败：{e}"}

    # 完整性预校验：所有归档文件 sha256 与清单一致后再落盘，
    # 避免在 overwrite 场景下用损坏/被篡改的数据覆盖掉用户既有数据库。
    file_entries = manifest.get("files", [])
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for entry in file_entries:
                arc = entry["archive_path"]
                if arc in SKIP_RESTORE_ENTRIES or not arc.startswith(DATA_PREFIX):
                    continue
                expected = entry.get("sha256")
                if not expected:
                    continue
                try:
                    actual = _sha256_bytes(zf.read(arc))
                except KeyError:
                    return {"success": False, "error": f"归档缺少文件 {arc}，可能已损坏。"}
                if actual != expected:
                    return {"success": False, "error": (
                        f"归档完整性校验失败：{arc} 内容与清单不符（sha256 不匹配），"
                        f"归档可能已损坏或被篡改。请用 session-archive-create 重新生成归档后再恢复。"
                    )}
    except zipfile.BadZipFile as e:
        return {"success": False, "error": f"归档文件损坏（读取失败）：{e}"}

    # 解压归档文件到目标位置（config.json 与 operation_logs.json 跳过，单独处理）
    restored_files = 0
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for entry in file_entries:
                arc = entry["archive_path"]
                if arc in SKIP_RESTORE_ENTRIES:
                    continue
                if not arc.startswith(DATA_PREFIX):
                    continue
                rel = arc[len(DATA_PREFIX):]
                target = os.path.join(audit_data_dir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(arc) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored_files += 1

            # 写入改写路径后的配置文件，使其指向恢复后的位置（相对 target_root）
            base_config = json.loads(zf.read("data/config.json").decode("utf-8"))
            data_subdir = os.path.basename(os.path.normpath(audit_data_dir))
            base_config.setdefault("database", {})["path"] = f"./{data_subdir}/audit.db"
            base_config.setdefault("export", {})["output_dir"] = f"./{data_subdir}/exports"
            base_config.pop("active_plan", None)
            base_config.pop("operator", None)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(base_config, f, ensure_ascii=False, indent=2)
    except zipfile.BadZipFile as e:
        return {"success": False, "error": f"归档文件损坏（解压失败）：{e}"}
    except OSError as e:
        return {"success": False, "error": f"恢复写盘失败：{e}。请检查目标目录是否可写或磁盘是否已满。"}

    # 关键动作写入恢复后的数据库日志
    logged = False
    try:
        if os.path.exists(db_path):
            db.restore_operation_log(
                db_path, plan_id=None, plan_name=None,
                operator=operator or manifest.get("operator") or "cli",
                action_type="session_restore",
                target_diff_id=None,
                action_data={
                    "archive_path": os.path.abspath(archive_path),
                    "target_root": os.path.abspath(target_root),
                    "conflict": conflict,
                    "renamed": renamed,
                    "restored_files": restored_files,
                    "source_created_at": manifest.get("created_at"),
                    "source_operator": manifest.get("operator"),
                },
                snapshot_before=None,
            )
            logged = True
    except Exception:
        pass

    row_counts = _db_row_counts(db_path) if os.path.exists(db_path) else {}

    return {
        "success": True,
        "target_root": os.path.abspath(target_root),
        "audit_data_dir": audit_data_dir,
        "db_path": db_path,
        "config_path": config_path,
        "renamed": renamed,
        "restored_files": restored_files,
        "row_counts": row_counts,
        "conflicts": conflicts,
        "logged": logged,
        "manifest": manifest,
    }
