"""执行归档与恢复模块 - 导出执行清单、从清单恢复执行历史.

归档清单内容：
- template_snapshot: 冻结的模板快照（含 id/name/version/filters/fields/steps/hash）
- steps: 每步的 step 定义、status、result、error
- operator: 操作人
- active_plan: 执行时激活的方案名
- export_files: 导出文件信息（路径、文件名、类型、模板版本）
- config_summary: 必要的配置摘要（csv 列名、状态列表、合并键、输出目录）
- execution_meta: execution_id、状态、时间戳、步数统计
- operation_logs: 相关导出操作日志
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import db
from . import templates as templates_mod


MANIFEST_VERSION = 1
MANIFEST_SCHEMA = "inventory_audit_execution_manifest"


def _ensure_archives_dir(config: Dict[str, Any]) -> str:
    """归档输出目录（与数据库同级的 archives/）."""
    archives_dir = os.path.join(
        os.path.dirname(os.path.abspath(config["database"]["path"])),
        "archives",
    )
    os.makedirs(archives_dir, exist_ok=True)
    return archives_dir


def _default_manifest_path(
    config: Dict[str, Any], execution_id: int, template_name: str,
) -> str:
    """默认归档文件路径：archives/exec_{id}_{tplname}_{timestamp}.json."""
    archives_dir = _ensure_archives_dir(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = template_name.replace(os.sep, "_").replace("/", "_").replace("\\", "_")
    return os.path.join(archives_dir, f"exec_{execution_id}_{safe_name}_{timestamp}.json")


def _collect_export_files(
    db_path: str, execution_id: int,
) -> List[Dict[str, Any]]:
    """收集某次执行相关的导出文件信息（从步骤结果和操作日志汇总）."""
    export_files: List[Dict[str, Any]] = []
    steps = db.get_steps(db_path, execution_id)
    seen_paths = set()
    for step_rec in steps:
        result = step_rec.get("result") or {}
        file_path = result.get("file_path")
        if file_path and file_path not in seen_paths:
            seen_paths.add(file_path)
            export_files.append({
                "step_index": step_rec.get("step_index"),
                "export_type": result.get("type"),
                "file_path": file_path,
                "filename": os.path.basename(file_path),
                "template_name": result.get("template_name"),
                "template_version": result.get("template_version"),
                "count": result.get("count"),
                "file_exists": os.path.exists(file_path),
                "file_size": os.path.getsize(file_path) if os.path.exists(file_path) else None,
            })
    return export_files


def _collect_operation_logs(
    db_path: str, template_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """收集相关导出操作日志（按模板名过滤），保留完整恢复所需字段."""
    logs = db.get_operation_logs(db_path, action_type="export")
    result = []
    for log in logs:
        action_data = log.get("action_data") or {}
        if template_name and action_data.get("template_name") != template_name:
            continue
        result.append({
            "log_id": log["id"],
            "plan_id": log.get("plan_id"),
            "plan_name": log.get("plan_name"),
            "operator": log.get("operator"),
            "action_type": log.get("action_type"),
            "target_diff_id": log.get("target_diff_id"),
            "action_data": action_data,
            "snapshot_before": log.get("snapshot_before"),
            "created_at": log.get("created_at"),
        })
    return result


def _config_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    """提取必要的配置摘要：csv 列、状态列表、合并键、输出目录."""
    csv_cfg = config.get("csv", {})
    status_cfg = config.get("status", {})
    rules_cfg = config.get("rules", {})
    export_cfg = config.get("export", {})
    return {
        "csv_columns": {
            "location": csv_cfg.get("location_column"),
            "sku": csv_cfg.get("sku_column"),
            "expected": csv_cfg.get("expected_column"),
            "counted": csv_cfg.get("counted_column"),
        },
        "status_initial": status_cfg.get("initial"),
        "status_allowed": status_cfg.get("allowed", []),
        "merge_keys": rules_cfg.get("merge_keys", ["location", "sku"]),
        "diff_threshold": rules_cfg.get("diff_threshold", 0),
        "export_output_dir": os.path.abspath(export_cfg.get("output_dir", "./audit_data/exports")),
    }


def export_execution_manifest(
    db_path: str,
    config: Dict[str, Any],
    execution_id: int,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """导出执行归档清单.

    Args:
        db_path: 数据库路径
        config: 全局配置
        execution_id: 执行记录 ID
        output_path: 输出文件路径，None 时自动生成

    Returns:
        {"success", "file_path", "manifest"}
    """
    execution = db.get_execution(db_path, execution_id)
    if not execution:
        return {"success": False, "error": f"执行记录不存在：{execution_id}"}

    steps = db.get_steps(db_path, execution_id)
    steps_payload = []
    for sr in steps:
        steps_payload.append({
            "step_index": sr.get("step_index"),
            "step": sr.get("step"),
            "status": sr.get("status"),
            "result": sr.get("result"),
            "error": sr.get("error"),
            "started_at": sr.get("started_at"),
            "finished_at": sr.get("finished_at"),
        })

    template_snapshot = execution.get("template_snapshot") or {}
    tpl_name = template_snapshot.get("name") or execution.get("template_name") or ""

    manifest = {
        "$schema": MANIFEST_SCHEMA,
        "$manifest_version": MANIFEST_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "execution_meta": {
            "execution_id": execution_id,
            "template_id": execution.get("template_id"),
            "template_name": execution.get("template_name"),
            "template_version": execution.get("template_version"),
            "status": execution.get("status"),
            "steps_total": execution.get("steps_total"),
            "steps_done": execution.get("steps_done"),
            "steps_failed": execution.get("steps_failed"),
            "started_at": execution.get("started_at"),
            "finished_at": execution.get("finished_at"),
            "operator": execution.get("operator"),
            "active_plan": execution.get("active_plan"),
        },
        "template_snapshot": template_snapshot,
        "steps": steps_payload,
        "operator": execution.get("operator") or config.get("operator", "cli"),
        "active_plan": execution.get("active_plan") or config.get("active_plan"),
        "export_files": _collect_export_files(db_path, execution_id),
        "operation_logs": _collect_operation_logs(db_path, template_name=tpl_name),
        "config_summary": _config_summary(config),
    }

    if output_path is None:
        output_path = _default_manifest_path(config, execution_id, tpl_name)

    abs_path = os.path.abspath(output_path)
    try:
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except OSError as e:
        return {"success": False, "error": f"写入归档文件失败：{e}"}

    return {"success": True, "file_path": abs_path, "manifest": manifest}


def load_manifest(manifest_path: str) -> Dict[str, Any]:
    """加载并校验归档清单文件."""
    if not os.path.exists(manifest_path):
        return {"success": False, "error": f"归档文件不存在：{manifest_path}"}
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"归档文件损坏（JSON 解析失败）：{e}"}
    except OSError as e:
        return {"success": False, "error": f"读取归档文件失败：{e}"}

    if not isinstance(manifest, dict):
        return {"success": False, "error": "归档文件格式损坏：根结构不是对象"}
    if manifest.get("$schema") != MANIFEST_SCHEMA:
        return {"success": False, "error": f"归档 schema 不匹配：{manifest.get('$schema')}"}
    if not manifest.get("template_snapshot"):
        return {"success": False, "error": "归档清单缺少 template_snapshot"}
    if "execution_meta" not in manifest:
        return {"success": False, "error": "归档清单缺少 execution_meta"}
    if "steps" not in manifest:
        return {"success": False, "error": "归档清单缺少 steps"}

    return {"success": True, "manifest": manifest}


def detect_restore_conflicts(
    db_path: str,
    config: Dict[str, Any],
    manifest: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """检测恢复冲突.

    冲突类型（全部为阻塞级，需显式 save-as 才能继续）：
    - template_upgraded: 同名模板已存在且版本/内容不一致
    - export_file_exists: 归档中的导出文件已存在于磁盘（会被误覆盖/混淆）
    - active_plan_mismatch: 当前激活方案与归档记录不一致

    每个冲突包含 resolution 字段，说明使用 --conflict save-as 时的处理方式。
    """
    conflicts: List[Dict[str, Any]] = []
    snap = manifest.get("template_snapshot") or {}
    tpl_name = snap.get("name") or manifest["execution_meta"].get("template_name")

    existing = templates_mod.get_template(db_path, config, tpl_name) if tpl_name else None
    if existing and snap:
        snap_hash = snap.get("content_hash")
        cur_hash = existing.get("content_hash")
        snap_version = int(snap.get("version") or 0)
        cur_version = int(existing.get("version") or 0)
        if snap_hash != cur_hash or snap_version != cur_version:
            conflicts.append({
                "type": "template_upgraded",
                "severity": "error",
                "message": (
                    f"同名模板 {tpl_name} 已升级："
                    f"归档 v{snap_version}（hash {snap_hash[:12] if snap_hash else '?'}）vs "
                    f"当前 v{cur_version}（hash {cur_hash[:12] if cur_hash else '?'}）"
                ),
                "resolution": (
                    f"使用 --conflict save-as 将另存为新模板 "
                    f"「{tpl_name}_restored」，不影响现有模板"
                ),
                "template_name": tpl_name,
                "archived_version": snap_version,
                "current_version": cur_version,
            })

    for ef in manifest.get("export_files", []):
        fp = ef.get("file_path")
        if fp and os.path.exists(fp):
            conflicts.append({
                "type": "export_file_exists",
                "severity": "error",
                "message": f"导出文件已存在：{fp}",
                "resolution": "使用 --conflict save-as 将仅恢复元数据，不覆盖磁盘现有文件",
                "file_path": fp,
                "filename": ef.get("filename"),
            })

    archived_plan = manifest.get("active_plan") or manifest["execution_meta"].get("active_plan")
    current_plan = config.get("active_plan")
    if archived_plan != current_plan:
        conflicts.append({
            "type": "active_plan_mismatch",
            "severity": "error",
            "message": (
                f"激活方案不一致：归档记录 {archived_plan or '(无)'} vs "
                f"当前 {current_plan or '(无)'}"
            ),
            "resolution": (
                f"使用 --conflict save-as 将按归档记录的方案 "
                f"「{archived_plan or '(无)'}」恢复执行记录，"
                f"不改动当前运行时激活方案「{current_plan or '(无)'}」"
            ),
            "archived_plan": archived_plan,
            "current_plan": current_plan,
        })

    return conflicts


BLOCKING_CONFLICT_TYPES = frozenset({
    "template_upgraded",
    "export_file_exists",
    "active_plan_mismatch",
})

SAVEAS_RESOLVABLE_TYPES = frozenset({
    "template_upgraded",
    "export_file_exists",
    "active_plan_mismatch",
})


def preview_manifest(
    manifest_path: str,
    db_path: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """预检/预览归档清单，不执行任何恢复操作.

    让用户在恢复前先看清归档里带了哪些执行信息、恢复后可能碰到什么冲突，
    再决定怎么继续。

    Args:
        manifest_path: 归档清单文件路径
        db_path: 数据库路径（用于冲突检测），None 时只预览内容不检测冲突
        config: 全局配置（用于冲突检测），None 时只预览内容不检测冲突

    Returns:
        {"success", "manifest", "preview", "conflicts", "suggestion"}
        - preview: 归档内容的人类可读摘要
        - conflicts: 检测到的冲突列表（仅当 db_path 和 config 都提供时）
        - suggestion: 建议的下一步操作
    """
    load_result = load_manifest(manifest_path)
    if not load_result.get("success"):
        return {"success": False, "error": load_result.get("error")}

    manifest = load_result["manifest"]
    meta = manifest.get("execution_meta", {})
    snap = manifest.get("template_snapshot", {})
    steps = manifest.get("steps", [])
    export_files = manifest.get("export_files", [])
    op_logs = manifest.get("operation_logs", [])
    filters = snap.get("filters", {})

    steps_summary = []
    for s in steps:
        step = s.get("step", {})
        action = step.get("action", "?")
        detail = step.get("type") or (",".join(step.get("action_types", [])) if step.get("action_types") else "")
        status = s.get("status", "pending")
        result = s.get("result", {})
        extra = ""
        if result.get("file_path"):
            extra = f" -> {os.path.basename(result['file_path'])}"
        elif result.get("count") is not None:
            extra = f" ({result['count']} 条)"
        steps_summary.append({
            "index": s.get("step_index"),
            "action": action,
            "detail": detail,
            "status": status,
            "extra": extra,
            "error": s.get("error"),
        })

    export_files_summary = []
    for ef in export_files:
        export_files_summary.append({
            "step_index": ef.get("step_index"),
            "type": ef.get("export_type"),
            "filename": ef.get("filename"),
            "file_path": ef.get("file_path"),
            "file_exists": ef.get("file_exists"),
            "file_size": ef.get("file_size"),
            "count": ef.get("count"),
        })

    preview = {
        "manifest_file": os.path.abspath(manifest_path),
        "manifest_version": manifest.get("$manifest_version"),
        "exported_at": manifest.get("exported_at"),
        "execution": {
            "id": meta.get("execution_id"),
            "status": meta.get("status"),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "steps_total": meta.get("steps_total"),
            "steps_done": meta.get("steps_done"),
            "steps_failed": meta.get("steps_failed"),
        },
        "template": {
            "id": snap.get("id") or meta.get("template_id"),
            "name": snap.get("name") or meta.get("template_name"),
            "version": snap.get("version") or meta.get("template_version"),
            "description": snap.get("description"),
            "content_hash": (snap.get("content_hash") or "")[:16] + "..." if snap.get("content_hash") else None,
            "filters": {
                "status": filters.get("status"),
                "location": filters.get("location"),
                "sku": filters.get("sku"),
            },
            "export_fields": snap.get("export_fields"),
            "remark_template": snap.get("remark_template"),
            "steps_count": len(snap.get("steps", [])),
        },
        "operator": manifest.get("operator") or meta.get("operator"),
        "active_plan": manifest.get("active_plan") or meta.get("active_plan"),
        "steps": steps_summary,
        "export_files": export_files_summary,
        "operation_logs_count": len(op_logs),
        "config_summary": manifest.get("config_summary", {}),
    }

    conflicts = []
    suggestion_parts = []

    if db_path and config:
        conflicts = detect_restore_conflicts(db_path, config, manifest)
        if conflicts:
            blocking = [c for c in conflicts if c["type"] in BLOCKING_CONFLICT_TYPES]
            if blocking:
                suggestion_parts.append(
                    "检测到阻塞冲突。建议："
                    "1) 使用 --conflict save-as 自动处理冲突；"
                    "2) 或手动清理冲突后再恢复。"
                )
            else:
                suggestion_parts.append("检测到非阻塞冲突，可正常恢复。")
        else:
            suggestion_parts.append("未检测到冲突，可直接恢复。")

        exec_status = meta.get("status", "")
        if exec_status in ("running", "failed", "interrupted"):
            suggestion_parts.append(
                f"原执行状态为「{exec_status}」，恢复后可使用 "
                f"'template-run {snap.get('name') or meta.get('template_name')} --resume' 续跑。"
            )
        suggestion_parts.append(
            f"恢复命令：template-restore-execution {os.path.basename(manifest_path)} "
            f"[--conflict abort|save-as]"
        )
    else:
        suggestion_parts.append(
            "仅预览内容，未检测冲突。如需检测冲突，请在有数据库的环境下运行预览。"
        )
        suggestion_parts.append(
            f"恢复命令：template-restore-execution {os.path.basename(manifest_path)}"
        )

    suggestion = " ".join(suggestion_parts)

    return {
        "success": True,
        "manifest": manifest,
        "preview": preview,
        "conflicts": conflicts,
        "suggestion": suggestion,
    }


def _restore_template_from_snapshot(
    db_path: str, config: Dict[str, Any], snap: Dict[str, Any],
    conflict_resolution: str = "abort",
) -> Dict[str, Any]:
    """从归档快照恢复模板.

    conflict_resolution:
    - "abort": 同名模板冲突时中止
    - "save-as" / "save_as": 冲突时另存为新模板名（<name>_restored）
    """
    if conflict_resolution == "save_as":
        conflict_resolution = "save-as"
    tpl_name = snap.get("name")
    if not tpl_name:
        return {"success": False, "error": "归档快照缺少模板名"}

    existing = templates_mod.get_template(db_path, config, tpl_name)
    snap_hash = templates_mod.compute_content_hash(
        snap.get("filters"), snap.get("export_fields"),
        snap.get("remark_template"), snap.get("steps"),
    )

    final_name = tpl_name
    restored_as_new = False

    if existing:
        existing_hash = existing.get("content_hash")
        existing_version = int(existing.get("version") or 0)
        snap_version = int(snap.get("version") or 0)
        if existing_hash == snap_hash and existing_version == snap_version:
            return {"success": True, "template": existing, "action": "matched_existing",
                    "name": tpl_name}
        if conflict_resolution == "abort":
            return {
                "success": False, "conflict": True, "conflict_type": "template_upgraded",
                "error": (
                    f"同名模板 {tpl_name} 已升级，恢复中止。"
                    f"使用 --conflict save-as 另存为新模板"
                ),
                "existing_name": tpl_name,
            }
        elif conflict_resolution == "save-as":
            final_name = f"{tpl_name}_restored"
            counter = 1
            while templates_mod.get_template(db_path, config, final_name):
                final_name = f"{tpl_name}_restored{counter}"
                counter += 1
            restored_as_new = True
        else:
            return {"success": False, "error": f"未知冲突处理策略：{conflict_resolution}"}

    save_result = templates_mod.save_template(
        db_path, config, final_name,
        filters=snap.get("filters"),
        export_fields=snap.get("export_fields"),
        remark_template=snap.get("remark_template"),
        steps=snap.get("steps"),
        description=snap.get("description") or (
            f"从归档恢复（原模板 {tpl_name} v{snap.get('version')}）"
            if restored_as_new else snap.get("description")
        ),
        force=False,
    )

    if not save_result.get("success"):
        return {"success": False, "error": f"恢复模板失败：{save_result.get('error')}"}

    restored = templates_mod.get_template(db_path, config, final_name)
    return {
        "success": True, "template": restored,
        "action": "save_as" if restored_as_new else save_result.get("action", "created"),
        "name": final_name, "original_name": tpl_name,
    }


def restore_execution_from_manifest(
    db_path: str,
    config: Dict[str, Any],
    manifest_path: str,
    conflict_resolution: str = "abort",
) -> Dict[str, Any]:
    """从归档清单恢复执行历史.

    Args:
        db_path: 数据库路径
        config: 全局配置
        manifest_path: 归档清单路径
        conflict_resolution: "abort" | "save-as"
            - abort: 检测到任意冲突（模板升级/文件已存在/方案不一致）立即中止
            - save-as:
                * 模板冲突时另存为新模板名（<name>_restored[1|2|…]）
                * 导出文件存在时仅恢复元数据不重写磁盘
                * 方案不一致时按归档记录的方案恢复（不改动当前激活方案）

    Returns:
        {"success", "execution_id", "template", "conflicts", ...}
    """
    if conflict_resolution == "save_as":
        conflict_resolution = "save-as"
    load_result = load_manifest(manifest_path)
    if not load_result.get("success"):
        return {"success": False, "error": load_result.get("error")}

    manifest = load_result["manifest"]
    conflicts = detect_restore_conflicts(db_path, config, manifest)

    blocking_conflicts = [c for c in conflicts if c["type"] in BLOCKING_CONFLICT_TYPES]
    if blocking_conflicts and conflict_resolution == "abort":
        return {
            "success": False, "conflict": True, "conflicts": conflicts,
            "error": (
                "检测到阻塞冲突，恢复中止。"
                "使用 --conflict save-as 处理：模板另存/保留现有文件/按归档方案恢复"
            ),
        }
    if blocking_conflicts and conflict_resolution not in ("abort", "save-as"):
        return {"success": False, "error": f"未知冲突处理策略：{conflict_resolution}"}

    snap = manifest["template_snapshot"]
    tpl_restore = _restore_template_from_snapshot(
        db_path, config, snap, conflict_resolution=conflict_resolution,
    )
    if not tpl_restore.get("success"):
        return {
            "success": False, "conflicts": conflicts,
            "error": tpl_restore.get("error"),
            "template_conflict": tpl_restore.get("conflict"),
        }

    restored_template = tpl_restore["template"]
    meta = manifest["execution_meta"]
    operator = manifest.get("operator") or meta.get("operator") or config.get("operator", "cli")
    active_plan = manifest.get("active_plan") or meta.get("active_plan")

    new_exec_id = db.create_execution(
        db_path,
        template_id=restored_template.get("id"),
        template_name=restored_template.get("name"),
        template_version=int(restored_template.get("version", 1) or 1),
        steps_total=len(manifest.get("steps", [])),
        template_snapshot=snap,
        operator=operator,
        active_plan=active_plan,
    )

    for step_payload in manifest.get("steps", []):
        idx = step_payload.get("step_index")
        if idx is None:
            continue
        db.upsert_step(
            db_path, new_exec_id, idx,
            step=step_payload.get("step") or {},
            status=step_payload.get("status", "pending"),
            result=step_payload.get("result"),
            error=step_payload.get("error"),
            finished=(step_payload.get("status") in ("done", "failed", "skipped_done")),
        )

    db.update_execution(
        db_path, new_exec_id,
        status=meta.get("status", "running"),
        steps_done=meta.get("steps_done", 0),
        steps_failed=meta.get("steps_failed", 0),
        finished=meta.get("status") in ("completed", "failed"),
    )

    logs_restored = 0
    for op_log in manifest.get("operation_logs", []):
        try:
            db.restore_operation_log(
                db_path,
                plan_id=None,
                plan_name=op_log.get("plan_name"),
                operator=op_log.get("operator"),
                action_type=op_log.get("action_type") or "export",
                target_diff_id=None,
                action_data=op_log.get("action_data") or {},
                snapshot_before=op_log.get("snapshot_before"),
                created_at=op_log.get("created_at"),
            )
            logs_restored += 1
        except Exception:
            pass

    return {
        "success": True,
        "execution_id": new_exec_id,
        "original_execution_id": meta.get("execution_id"),
        "template": restored_template,
        "name": tpl_restore.get("name"),
        "original_name": tpl_restore.get("original_name"),
        "template_action": tpl_restore.get("action"),
        "conflicts": conflicts,
        "steps_restored": len(manifest.get("steps", [])),
        "logs_restored": logs_restored,
    }
