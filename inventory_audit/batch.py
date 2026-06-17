"""模板批量执行引擎 - 按模板步骤顺序执行 list/export/replay.

核心保证：
1. 每次执行生成 execution 记录，冻结模板快照（filters/fields/steps/version），
   模板后续更新不会静默改写已有执行记录；
2. 某步失败时前面已完成步骤保留，执行标记为 failed；
3. 重试（resume 同一 execution_id）时，status=done 的步骤直接跳过，
   不重复产生日志或导出物；status=failed/pending 的步骤才会重跑。
"""
from typing import Any, Dict, List, Optional

from . import db
from . import exporter
from . import merger
from . import replay as replay_mod
from . import templates as templates_mod


def _snapshot_of(template: Dict[str, Any]) -> Dict[str, Any]:
    """从模板抽取可执行快照（去除时间戳，保留版本与内容指纹）."""
    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "version": template.get("version", 1),
        "description": template.get("description"),
        "filters": template.get("filters") or {},
        "export_fields": template.get("export_fields"),
        "remark_template": template.get("remark_template"),
        "steps": template.get("steps") or [],
        "content_hash": template.get("content_hash"),
    }


def _run_step(
    db_path: str,
    config: Dict[str, Any],
    step: Dict[str, Any],
    template: Dict[str, Any],
    output_dir: str,
    allowed_statuses: Optional[List[str]],
    operator: str,
) -> Dict[str, Any]:
    """执行单步，返回带 success 标记的结果字典."""
    action = step.get("action")
    filters = (template.get("filters") or {}) if template else {}
    status = filters.get("status")
    location = filters.get("location")
    sku = filters.get("sku")

    if action == "list":
        diffs = merger.get_merged_differences(
            db_path, status=status, location=location, sku=sku,
        )
        return {
            "success": True, "action": "list",
            "count": len(diffs),
            "filters": {"status": status, "location": location, "sku": sku},
        }

    if action == "export":
        export_type = step.get("type", "differences")
        if export_type == "differences":
            r = exporter.export_differences(
                db_path, output_dir,
                status=status, location=location, sku=sku,
                include_sources=True, template=template, operator=operator,
            )
        elif export_type == "summary":
            r = exporter.export_summary(
                db_path, output_dir, template=template, operator=operator,
            )
        elif export_type == "sources":
            r = exporter.export_source_lines(
                db_path, output_dir, batch_id=step.get("batch_id"),
                template=template, operator=operator,
            )
        else:
            return {"success": False, "action": "export",
                    "error": f"未知 export 类型：{export_type}"}
        if r.get("success"):
            return {
                "success": True, "action": "export", "type": export_type,
                "file_path": r.get("file_path"), "count": r.get("count", 0),
                "template_name": r.get("template_name"),
                "template_version": r.get("template_version"),
            }
        return {
            "success": False, "action": "export", "type": export_type,
            "error": r.get("error", "导出失败"),
        }

    if action == "replay":
        action_types = step.get("action_types")
        resolution = step.get("resolution", "keep")
        r = replay_mod.replay_operations(
            db_path, output_dir,
            action_types=action_types,
            allowed_statuses=allowed_statuses,
            default_conflict_resolution=resolution,
            config_for_plan_lookup=config,
        )
        if r.get("success"):
            return {
                "success": True, "action": "replay",
                "replayed": len(r.get("replayed", [])),
                "exports": len(r.get("exports", [])),
                "conflicts": len(r.get("conflicts", [])),
                "skipped": len(r.get("skipped", [])),
            }
        aborted = r.get("aborted") or {}
        return {
            "success": False, "action": "replay",
            "error": aborted.get("message", "回放因冲突中止"),
            "aborted": aborted,
        }

    return {"success": False, "error": f"未知步骤 action：{action}"}


def run_template(
    db_path: str,
    config: Dict[str, Any],
    template: Optional[Dict[str, Any]],
    output_dir: str,
    allowed_statuses: Optional[List[str]] = None,
    operator: str = "cli",
    execution_id: Optional[int] = None,
) -> Dict[str, Any]:
    """按模板步骤批量执行，支持续跑（resume）.

    Args:
        db_path: 数据库路径
        config: 全局配置
        template: 模板对象（新建执行时用；续跑时优先用冻结快照）
        output_dir: 导出/快照输出目录
        allowed_statuses: 允许的状态列表（replay 步骤需要）
        operator: 操作人
        execution_id: 续跑时传入已存在的执行记录 ID；None 表示新建

    Returns:
        {"success", "execution_id", "status", "steps", "steps_total",
         "steps_done", "steps_failed", "version_drift"}
    """
    if execution_id is not None:
        execution = db.get_execution(db_path, execution_id)
        if not execution:
            return {"success": False, "error": f"执行记录不存在：{execution_id}"}
        snapshot = execution.get("template_snapshot") or {}
        steps = list(snapshot.get("steps") or [])
        if not steps:
            step_records = db.get_steps(db_path, execution_id)
            steps = [sr["step"] for sr in step_records]
        frozen_template = snapshot if snapshot else (template or {})
        drift = templates_mod.check_version_drift(db_path, config, execution)
    else:
        if not template:
            return {"success": False, "error": "新建执行必须提供模板"}
        snapshot = _snapshot_of(template)
        steps = list(template.get("steps") or [])
        execution_id = db.create_execution(
            db_path, template.get("id"), template.get("name"),
            int(template.get("version", 1) or 1), len(steps),
            template_snapshot=snapshot,
        )
        for i, step in enumerate(steps):
            db.upsert_step(db_path, execution_id, i, step, "pending")
        frozen_template = snapshot
        drift = None

    results: List[Dict[str, Any]] = []
    done = 0
    failed = 0
    interrupted = False

    for i, step in enumerate(steps):
        step_record = db.get_step(db_path, execution_id, i)
        if step_record and step_record.get("status") == "done":
            done += 1
            results.append({
                "step_index": i, "step": step, "status": "skipped_done",
                "result": step_record.get("result"),
            })
            continue

        try:
            result = _run_step(
                db_path, config, step, frozen_template,
                output_dir, allowed_statuses, operator,
            )
        except Exception as e:  # noqa: BLE001 - 中断后需保留已完成步骤
            db.upsert_step(db_path, execution_id, i, step, "failed",
                           error=str(e), finished=True)
            results.append({
                "step_index": i, "step": step, "status": "failed",
                "error": str(e),
            })
            failed += 1
            interrupted = True
            break

        if result.get("success"):
            db.upsert_step(db_path, execution_id, i, step, "done",
                           result=result, finished=True)
            done += 1
            results.append({
                "step_index": i, "step": step, "status": "done",
                "result": result,
            })
        else:
            db.upsert_step(db_path, execution_id, i, step, "failed",
                           result=result, error=result.get("error"),
                           finished=True)
            results.append({
                "step_index": i, "step": step, "status": "failed",
                "error": result.get("error"), "result": result,
            })
            failed += 1
            interrupted = True
            break

    status = "failed" if interrupted else "completed"
    db.update_execution(
        db_path, execution_id, status=status,
        steps_done=done, steps_failed=failed, finished=True,
    )

    return {
        "success": not interrupted,
        "execution_id": execution_id,
        "status": status,
        "steps": results,
        "steps_total": len(steps),
        "steps_done": done,
        "steps_failed": failed,
        "version_drift": drift,
    }
