"""CLI 入口 - 仓库盘点差异复核工具."""
import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from . import archive as archive_mod
from . import batch as batch_mod
from . import batch_templates as bt_mod
from . import config as cfg
from . import db
from . import exporter
from . import importer
from . import merger
from . import plans as plans_mod
from . import replay as replay_mod
from . import reviewer
from . import session_archive as session_archive_mod
from . import templates as templates_mod


def _load_config(args) -> Dict[str, Any]:
    """加载配置并确保目录存在."""
    config = cfg.load_config(getattr(args, "config", None))
    cfg.ensure_dirs(config)
    return config


def _get_db_path(config: Dict[str, Any]) -> str:
    return cfg.get_db_path(config)


def _launcher_path() -> str:
    """返回仓库根目录下 inventory-audit 启动器的绝对路径.

    用于 session-archive-restore 恢复后给用户打印可直接执行的后续命令——
    统一指向启动器，避免提示 `python -m inventory_audit` 却在离开源码树后失效。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "inventory-audit")


def _resolve_current_plan(config: Dict[str, Any], db_path: str) -> Optional[Dict[str, Any]]:
    """根据 config.active_plan 解析出方案对象；不存在或未设置返回 None."""
    plan_name = config.get("active_plan")
    if not plan_name:
        return None
    return plans_mod.get_plan(db_path, config, plan_name)


def cmd_init(args) -> int:
    """初始化命令 - 创建数据库和目录."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)
    cfg.save_runtime_state(config)
    print(f"[OK] 初始化完成")
    print(f"  数据库: {db_path}")
    print(f"  导出目录: {cfg.get_export_dir(config)}")
    print(f"  操作人: {config.get('operator', 'cli')}")
    return 0


def cmd_import(args) -> int:
    """导入 CSV 命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    csv_path = args.csv_file
    batch_name = getattr(args, "name", None)

    batches_before = db.list_batches(db_path)
    batch_ids_before = {b["id"] for b in batches_before}
    summary_before = db.get_summary(db_path)

    result = importer.import_csv(
        db_path, csv_path, config["csv"],
        batch_name=batch_name,
        default_status=config["status"]["initial"],
        rules=cfg.get_rules(config),
    )

    if not result["success"]:
        if result.get("duplicate"):
            print(f"[WARN] 文件已导入过: {result['error']}")
            return 0
        print(f"[ERROR] 导入失败: {result['error']}")
        if result.get("errors"):
            for err in result["errors"][:10]:
                print(f"  - {err}")
            if len(result["errors"]) > 10:
                print(f"  ... 还有 {len(result['errors']) - 10} 条错误")
        return 1

    batches_after = db.list_batches(db_path)
    summary_after = db.get_summary(db_path)

    untouched = all(b["id"] in batch_ids_before or b["id"] == result["batch_id"] for b in batches_after)
    assert untouched, "导入过程不应修改旧批次 ID"
    assert summary_before["batch_count"] <= summary_after["batch_count"], "批次数量不应减少"

    print(f"[OK] 导入成功（方案切换不串改旧批次与汇总）")
    print(f"  批次 ID: {result['batch_id']}")
    print(f"  批次名称: {result['batch_name']}")
    print(f"  导入差异行: {result['imported']}")
    print(f"  零差异跳过: {result['zero_diff_skipped']}")
    if result.get("below_threshold_skipped", 0) > 0:
        print(f"  低于阈值跳过: {result['below_threshold_skipped']}")

    if result.get("error_count", 0) > 0:
        print(f"  数据错误: {result['error_count']} 条")
        for err in result["errors"][:5]:
            print(f"    - {err}")
        if result["error_count"] > 5:
            print(f"    ... 还有 {result['error_count'] - 5} 条")

    return 0


def cmd_list(args) -> int:
    """列出差异命令（支持当前方案筛选 + 显式参数覆盖）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    current_plan = _resolve_current_plan(config, db_path)

    status = getattr(args, "status", None)
    location = getattr(args, "location", None)
    sku = getattr(args, "sku", None)

    filters = plans_mod.apply_plan_filters(current_plan, status, location, sku)

    diffs = merger.get_merged_differences(
        db_path, status=filters["status"], location=filters["location"], sku=filters["sku"]
    )

    if not diffs:
        print("没有找到差异记录")
        if current_plan:
            print(f"  [使用方案: {current_plan['name']}]")
        return 0

    if current_plan:
        print(f"[方案: {current_plan['name']}] 共 {len(diffs)} 条差异:")
    else:
        print(f"共 {len(diffs)} 条差异:")
    print("-" * 80)
    print(f"{'ID':<5} {'库位':<12} {'SKU':<15} {'差异数':>8} {'状态':<8} {'来源':>5} {'备注':<20}")
    print("-" * 80)

    for d in diffs:
        status_label = reviewer.get_status_label(d["status"])
        remark = (d.get("remark") or "")[:18]
        print(
            f"{d['id']:<5} {d['location']:<12} {d['sku']:<15} "
            f"{d['total_diff_qty']:>8.0f} {status_label:<8} "
            f"{d['source_count']:>5} {remark:<20}"
        )

    return 0


def cmd_show(args) -> int:
    """查看差异详情命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    diff_id = args.diff_id
    diff = merger.get_diff_detail(db_path, diff_id)

    if not diff:
        print(f"[ERROR] 差异 {diff_id} 不存在")
        return 1

    print(f"=== 差异详情 #{diff_id} ===")
    print(f"库位: {diff['location']}")
    print(f"SKU: {diff['sku']}")
    print(f"差异数量: {diff['total_diff_qty']}")
    print(f"状态: {reviewer.get_status_label(diff['status'])}")
    print(f"备注: {diff.get('remark') or '(空)'}")
    print(f"创建时间: {diff['created_at']}")
    print(f"更新时间: {diff['updated_at']}")

    sources = diff.get("sources", [])
    print(f"\n来源行 ({len(sources)} 条):")
    for src in sources:
        print(f"  [{src['batch_name']}] 行{src['line_number']}: "
              f"账面{src['expected_qty']} / 实盘{src['counted_qty']} "
              f"(差异{src['diff_qty']})")

    history = diff.get("history", [])
    if history:
        print(f"\n复核历史 ({len(history)} 条):")
        for h in history:
            action = h["action_type"]
            if action == "status_change":
                desc = f"状态: {h['old_status']} -> {h['new_status']}"
            elif action == "remark_change":
                desc = "备注变更"
            else:
                desc = action
            plan_part = f" [方案:{h.get('plan_name')}]" if h.get("plan_name") else ""
            print(f"  [{h['created_at']}] {desc} ({h['operator']}){plan_part}")

    return 0


def cmd_status(args) -> int:
    """设置状态命令（记录当前方案和操作人）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    current_plan = _resolve_current_plan(config, db_path)

    diff_ids = args.diff_ids
    status = args.status
    allowed = cfg.get_allowed_statuses(config)
    operator = config.get("operator", "cli")
    plan_id = current_plan["id"] if current_plan else None
    plan_name = current_plan["name"] if current_plan else None

    if len(diff_ids) == 1:
        result = reviewer.set_status(
            db_path, diff_ids[0], status, operator=operator,
            allowed_statuses=allowed, plan_id=plan_id, plan_name=plan_name,
        )
    else:
        result = reviewer.batch_set_status(
            db_path, diff_ids, status, operator=operator,
            allowed_statuses=allowed, plan_id=plan_id, plan_name=plan_name,
        )

    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '操作失败')}")
        return 1

    if result.get("skipped"):
        print(f"[SKIP] {result.get('message', '')}")
        return 0

    if "updated" in result:
        print(f"[OK] 批量更新完成")
        print(f"  成功: {result['updated']} / {result['total']}")
        if result.get("failed"):
            print(f"  失败: {len(result['failed'])}")
            for f in result["failed"][:5]:
                print(f"    - ID {f['id']}: {f['error']}")
    else:
        print(f"[OK] 状态已更新")
        print(f"  差异 #{result['diff_id']}: "
              f"{result['old_status']} -> {result['new_status']}")
        if plan_name:
            print(f"  方案: {plan_name}")
    return 0


def cmd_remark(args) -> int:
    """设置备注命令（记录当前方案和操作人，可套用备注模板）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    current_plan = _resolve_current_plan(config, db_path)

    diff_id = args.diff_id
    remark = args.remark

    if current_plan and current_plan.get("remark_template") and "{diff_id}" in remark:
        remark = remark.format(
            diff_id=diff_id,
            template=current_plan.get("remark_template", ""),
        )
    elif current_plan and current_plan.get("remark_template") and not remark:
        remark = current_plan["remark_template"]

    operator = config.get("operator", "cli")
    plan_id = current_plan["id"] if current_plan else None
    plan_name = current_plan["name"] if current_plan else None

    result = reviewer.set_remark(
        db_path, diff_id, remark, operator=operator,
        plan_id=plan_id, plan_name=plan_name,
    )

    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '操作失败')}")
        return 1

    if result.get("skipped"):
        print(f"[SKIP] {result.get('message', '')}")
        return 0

    print(f"[OK] 备注已更新")
    print(f"  差异 #{result['diff_id']}")
    if plan_name:
        print(f"  方案: {plan_name}")
    return 0


def cmd_undo(args) -> int:
    """撤销命令（记录当前方案和操作人）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    current_plan = _resolve_current_plan(config, db_path)

    operator = config.get("operator", "cli")
    plan_id = current_plan["id"] if current_plan else None
    plan_name = current_plan["name"] if current_plan else None

    result = reviewer.undo_last(
        db_path, operator=operator,
        plan_id=plan_id, plan_name=plan_name,
    )

    if not result.get("success"):
        if result.get("empty_history"):
            print("[INFO] 没有可撤销的操作")
            return 0
        print(f"[ERROR] {result.get('error', '撤销失败')}")
        return 1

    print(f"[OK] 已撤销")
    print(f"  差异 #{result['diff_id']}")
    print(f"  操作类型: {result['action_type']}")
    print(f"  {result.get('description', '')}")
    return 0


def cmd_history(args) -> int:
    """查看历史命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    diff_id = getattr(args, "diff_id", None)
    limit = getattr(args, "limit", 20)

    history = reviewer.get_review_history(db_path, diff_id=diff_id, limit=limit)

    if not history:
        print("暂无复核历史")
        return 0

    print(f"最近 {len(history)} 条复核历史:")
    print("-" * 70)
    for h in history:
        action = h["action_type"]
        if action == "status_change":
            desc = f"状态 {h['old_status']} -> {h['new_status']}"
        elif action == "remark_change":
            desc = "备注变更"
        else:
            desc = action
        plan_part = f" [方案:{h.get('plan_name')}]" if h.get("plan_name") else ""
        print(f"[{h['created_at']}] #{h['difference_id']} "
              f"({h['location']}/{h['sku']}): {desc} ({h['operator']}){plan_part}")

    return 0


def cmd_export(args) -> int:
    """导出命令（支持当前方案的导出字段和状态过滤）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    current_plan = _resolve_current_plan(config, db_path)

    output_dir = cfg.get_export_dir(config)
    status = getattr(args, "status", None)
    export_type = getattr(args, "type", "differences")
    operator = config.get("operator", "cli")

    filters = plans_mod.apply_plan_filters(current_plan, status, None, None)
    effective_status = filters["status"]
    effective_location = filters["location"]
    effective_sku = filters["sku"]

    if export_type == "differences":
        result = exporter.export_differences(
            db_path, output_dir,
            status=effective_status,
            location=effective_location,
            sku=effective_sku,
            include_sources=True, plan=current_plan, operator=operator,
        )
    elif export_type == "summary":
        result = exporter.export_summary(
            db_path, output_dir, plan=current_plan, operator=operator,
        )
    elif export_type == "sources":
        batch_id = getattr(args, "batch_id", None)
        result = exporter.export_source_lines(
            db_path, output_dir, batch_id=batch_id,
            plan=current_plan, operator=operator,
        )
    else:
        print(f"[ERROR] 未知导出类型: {export_type}")
        return 1

    if not result["success"]:
        print(f"[ERROR] {result.get('error', '导出失败')}")
        return 1

    print(f"[OK] 导出成功")
    print(f"  文件: {result['file_path']}")
    if "count" in result:
        print(f"  记录数: {result['count']}")
    if result.get("plan_name"):
        print(f"  方案: {result['plan_name']}")
    return 0


def cmd_batches(args) -> int:
    """列出批次命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    batches = db.list_batches(db_path)

    if not batches:
        print("暂无批次")
        return 0

    print(f"共 {len(batches)} 个批次:")
    print("-" * 70)
    print(f"{'ID':<4} {'名称':<20} {'导入时间':<20} {'行数':>6} {'状态':<8}")
    print("-" * 70)
    for b in batches:
        count = db.get_batch_source_count(db_path, b["id"])
        print(f"{b['id']:<4} {b['batch_name']:<20} {b['imported_at']:<20} "
              f"{count:>6} {b['status']:<8}")

    return 0


def cmd_summary(args) -> int:
    """汇总命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    summary = merger.get_merge_summary(db_path)

    print("=== 汇总统计 ===")
    print(f"差异总数: {summary['total_differences']}")
    print(f"差异绝对值总量: {summary['total_abs_qty']}")
    print(f"批次数量: {summary['batch_count']}")
    print(f"多来源差异: {summary['multi_source_differences']}")
    print(f"单来源差异: {summary['single_source_differences']}")

    print("\n按状态统计:")
    for status, data in summary.get("by_status", {}).items():
        label = reviewer.get_status_label(status)
        print(f"  {label}: {data['count']} 条, 差异量 {data['qty']}")

    return 0


def cmd_remerge(args) -> int:
    """重新合并命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    force = getattr(args, "force", False)
    if not force:
        print("[WARN] 重新合并会重建差异表，状态和备注会保留。")
        confirm = input("确认继续? (y/N): ")
        if confirm.lower() != "y":
            print("已取消")
            return 0

    result = merger.remerge_all(
        db_path,
        merge_keys=cfg.get_rules(config)["merge_keys"],
    )

    print(f"[OK] 重新合并完成")
    print(f"  差异总数: {result['total_differences']}")
    print(f"  保留状态: {result['preserved_status']}")
    print(f"  来源行数: {result['source_lines']}")
    return 0


def cmd_plan_save(args) -> int:
    """保存复核方案."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    status = getattr(args, "status", None)
    location = getattr(args, "location", None)
    sku = getattr(args, "sku", None)
    export_fields = getattr(args, "fields", None)
    if export_fields:
        export_fields = [f.strip() for f in export_fields.split(",") if f.strip()]
    remark_template = getattr(args, "remark_template", None)

    result = plans_mod.save_plan(
        db_path, config, name,
        filter_status=status, filter_location=location, filter_sku=sku,
        export_fields=export_fields, remark_template=remark_template,
    )

    print(f"[OK] 方案已保存: {name} (ID={result['plan_id']})")
    if result["plan"].get("filter_status"):
        print(f"  状态过滤: {result['plan']['filter_status']}")
    if result["plan"].get("filter_location"):
        print(f"  库位过滤: {result['plan']['filter_location']}")
    if result["plan"].get("filter_sku"):
        print(f"  SKU 过滤: {result['plan']['filter_sku']}")
    if result["plan"].get("export_fields"):
        print(f"  导出字段: {', '.join(result['plan']['export_fields'])}")
    if result["plan"].get("remark_template"):
        print(f"  备注模板: {result['plan']['remark_template']}")
    return 0


def cmd_plan_list(args) -> int:
    """列出所有复核方案."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    plans = plans_mod.list_plans(db_path)

    if not plans:
        print("暂无方案")
        return 0

    active = config.get("active_plan")
    print(f"共 {len(plans)} 个方案（当前激活: {active or '(无)'}）:")
    print("-" * 80)
    print(f"{'ID':<4} {'名称':<20} {'状态过滤':<10} {'更新时间':<20}")
    print("-" * 80)
    for p in plans:
        mark = "*" if p["name"] == active else " "
        print(f"{mark}{p['id']:<3} {p['name']:<20} "
              f"{(p.get('filter_status') or '-'):<10} "
              f"{p.get('updated_at', ''):<20}")
    return 0


def cmd_plan_use(args) -> int:
    """激活指定方案（重启后续用）."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    name = args.name
    if name is None:
        cfg.set_active_plan(config, None)
        print("[OK] 已清除激活方案")
        return 0

    plan = plans_mod.get_plan(db_path, config, name)
    if not plan:
        print(f"[ERROR] 方案不存在: {name}")
        return 1

    cfg.set_active_plan(config, name)
    print(f"[OK] 已激活方案: {name}（重启后仍然生效）")
    return 0


def cmd_plan_delete(args) -> int:
    """删除方案."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    name = args.name
    ok = plans_mod.delete_plan(db_path, config, name)
    if not ok:
        print(f"[WARN] 方案不存在或删除失败: {name}")
        return 0

    if config.get("active_plan") == name:
        cfg.set_active_plan(config, None)
        print(f"[OK] 方案 {name} 已删除（同时清除激活）")
    else:
        print(f"[OK] 方案 {name} 已删除")
    return 0


def cmd_set_operator(args) -> int:
    """设置操作人（重启后续用）."""
    config = _load_config(args)
    operator = args.operator
    cfg.set_operator(config, operator)
    print(f"[OK] 操作人已设为: {operator}（重启后仍然生效）")
    return 0


def cmd_replay(args) -> int:
    """回放操作日志（带冲突检测）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    output_dir = cfg.get_export_dir(config)
    allowed = cfg.get_allowed_statuses(config)

    plan_name = getattr(args, "plan", None)
    operator = getattr(args, "operator", None)
    resolution = getattr(args, "resolution", "abort")
    action_types_raw = getattr(args, "action_types", None)
    action_types = None
    if action_types_raw:
        action_types = [a.strip() for a in action_types_raw.split(",") if a.strip()]

    plan_id = None
    if plan_name:
        plan = plans_mod.get_plan(db_path, config, plan_name)
        if not plan:
            print(f"[ERROR] 方案不存在: {plan_name}")
            return 1
        plan_id = plan["id"]

    result = replay_mod.replay_operations(
        db_path, output_dir,
        plan_id=plan_id, plan_name=plan_name, operator=operator,
        allowed_statuses=allowed,
        default_conflict_resolution=resolution,
        action_types=action_types,
        config_for_plan_lookup=config,
    )

    if not result["success"] and result.get("aborted"):
        c = result["aborted"]
        print(f"[ABORT] 回放因冲突中止：{c['message']}")
        print(f"  日志 ID: {c['log_id']}")
        print(f"  原因: {c['reason']}")
        if len(result["replayed"]) > 0:
            print(f"  已成功回放: {len(result['replayed'])} 条")
        return 2

    print(f"[OK] 回放完成")
    print(f"  成功: {len(result['replayed'])} 条")
    if result["conflicts"]:
        print(f"  冲突: {len(result['conflicts'])} 条")
        for c in result["conflicts"]:
            print(f"    - log#{c['log_id']}: {c['message']}")
    if result["skipped"]:
        print(f"  跳过: {len(result['skipped'])} 条")
    if result["exports"]:
        print(f"  重新导出: {len(result['exports'])} 份")
        for e in result["exports"]:
            print(f"    - {e.get('file_path')}")
    return 0


def _parse_steps(args) -> Optional[List[Dict[str, Any]]]:
    """从 --steps-file(JSON) 或 --steps(逗号简写) 解析步骤；均为空时返回默认步骤.

    简写：list / export(=differences) / export:summary / export:sources / replay
    """
    steps_file = getattr(args, "steps_file", None)
    if steps_file:
        try:
            with open(steps_file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[ERROR] 读取步骤文件失败：{e}")
            return None
        if not isinstance(data, list):
            print(f"[ERROR] 步骤文件根结构必须是数组：{steps_file}")
            return None
        return data

    steps_str = getattr(args, "steps", None)
    if steps_str:
        steps: List[Dict[str, Any]] = []
        for token in steps_str.split(","):
            token = token.strip()
            if not token:
                continue
            if token == "list":
                steps.append({"action": "list"})
            elif token == "export":
                steps.append({"action": "export", "type": "differences"})
            elif token.startswith("export:"):
                steps.append({"action": "export", "type": token.split(":", 1)[1]})
            elif token == "replay":
                steps.append({"action": "replay"})
            else:
                print(f"[ERROR] 未知步骤简写：{token}（允许 list/export/export:summary/replay）")
                return None
        return steps

    return [
        {"action": "list"},
        {"action": "export", "type": "differences"},
        {"action": "export", "type": "summary"},
    ]


def cmd_template_save(args) -> int:
    """保存复核方案模板（筛选/导出字段/回放动作，版本化）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    filters = {
        "status": getattr(args, "status", None),
        "location": getattr(args, "location", None),
        "sku": getattr(args, "sku", None),
    }
    export_fields = getattr(args, "fields", None)
    if export_fields:
        export_fields = [f.strip() for f in export_fields.split(",") if f.strip()]
    remark_template = getattr(args, "remark_template", None)
    description = getattr(args, "description", None)
    force = getattr(args, "force", False)

    steps = _parse_steps(args)
    if steps is None:
        return 1

    result = templates_mod.save_template(
        db_path, config, name,
        filters=filters, export_fields=export_fields,
        remark_template=remark_template, steps=steps,
        description=description, force=force,
    )

    if not result.get("success"):
        if result.get("conflict"):
            print(f"[冲突] {result['message']}")
            print(f"  已存在版本: v{result.get('existing_version')}")
            print(f"  使用 --force 显式覆盖（将 bump 版本，已有执行记录保留旧版本）")
        else:
            print(f"[ERROR] {result.get('error', '保存失败')}")
        return 1

    action = result.get("action", "created")
    tag = {"created": "已创建", "overwritten": "已覆盖更新", "unchanged": "无变更"}[action]
    print(f"[OK] 模板 {name} {tag}（v{result['version']}，ID={result['template_id']}）")
    print(f"  步骤数: {len(steps)}")
    for i, s in enumerate(steps):
        print(f"    {i}. {s.get('action')}{'/' + s.get('type','') if s.get('type') else ''}")
    return 0


def cmd_template_list(args) -> int:
    """列出所有模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    templates = templates_mod.list_templates(db_path)
    if not templates:
        print("暂无模板")
        return 0

    print(f"共 {len(templates)} 个模板:")
    print("-" * 80)
    print(f"{'ID':<4} {'名称':<20} {'版本':<6} {'步骤':<4} {'更新时间':<20}")
    print("-" * 80)
    for t in templates:
        print(f"{t['id']:<4} {t['name']:<20} v{t.get('version', 1):<5} "
              f"{len(t.get('steps', [])):<4} {t.get('updated_at', ''):<20}")
    return 0


def cmd_template_show(args) -> int:
    """查看模板详情（含冻结的执行记录与版本漂移检查）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    template = templates_mod.get_template(db_path, config, name)
    if not template:
        print(f"[ERROR] 模板不存在：{name}")
        return 1

    print(f"=== 模板: {template['name']} (ID={template['id']}, v{template.get('version', 1)}) ===")
    if template.get("description"):
        print(f"描述: {template['description']}")
    filters = template.get("filters", {})
    print(f"筛选: status={filters.get('status') or '-'}, "
          f"location={filters.get('location') or '-'}, sku={filters.get('sku') or '-'}")
    if template.get("export_fields"):
        print(f"导出字段: {', '.join(template['export_fields'])}")
    if template.get("remark_template"):
        print(f"备注模板: {template['remark_template']}")
    print(f"内容指纹: {template.get('content_hash', '')[:16]}...")
    print(f"\n执行步骤 ({len(template.get('steps', []))} 步):")
    for i, s in enumerate(template.get("steps", [])):
        detail = s.get("type") or (",".join(s.get("action_types", [])) if s.get("action_types") else "")
        print(f"  {i}. {s.get('action')}" + (f" [{detail}]" if detail else ""))

    execs = db.list_executions(db_path, template_id=template["id"])
    if execs:
        print(f"\n执行记录 ({len(execs)} 条) — 状态说明: completed=[OK]已完成 / failed=[!!]中断可续跑 / running=[>>]执行中 / pending=[..]待执行")
        for e in execs:
            status = e.get("status", "?")
            status_cn = {
                "completed": "[OK]已完成",
                "failed": "[!!]中断(可续跑)",
                "running": "[>>]执行中",
                "pending": "[..]待执行",
            }.get(status, status)
            op_part = f" op={e.get('operator') or '-'}"
            plan_part = f" plan={e.get('active_plan') or '-'}"
            print(f"  #{e['id']} v{e.get('template_version')} "
                  f"{status_cn:<16} "
                  f"完成 {e.get('steps_done')}/{e.get('steps_total')} "
                  f"失败 {e.get('steps_failed')}"
                  f"{op_part}{plan_part}")
            if status != "completed":
                print(f"         → 可续跑: template-run {template['name']} --execution-id {e['id']}")
    return 0


def cmd_template_delete(args) -> int:
    """删除模板（数据库 + JSON 文件）."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    name = args.name
    ok = templates_mod.delete_template(db_path, config, name)
    if not ok:
        print(f"[WARN] 模板不存在或删除失败：{name}")
        return 0
    print(f"[OK] 模板 {name} 已删除")
    return 0


def cmd_template_import(args) -> int:
    """从 JSON 配置文件导入模板（处理重名与内容冲突）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    force = getattr(args, "force", False)
    result = templates_mod.import_template(db_path, config, args.file, force=force)
    if not result.get("success"):
        if result.get("conflict"):
            print(f"[冲突] {result['message']}")
            print(f"  已存在版本: v{result.get('existing_version')}")
            print(f"  使用 --force 显式覆盖")
        else:
            print(f"[ERROR] {result.get('error', '导入失败')}")
        return 1

    action = result.get("action", "created")
    tag = {"created": "已导入（新建）", "overwritten": "已覆盖更新",
           "unchanged": "内容未变化"}[action]
    print(f"[OK] 模板 {result.get('message', '')}")
    print(f"  操作: {tag}，当前版本 v{result.get('version')}")
    return 0


def cmd_template_export(args) -> int:
    """把模板导出为 JSON 配置文件（可分享/备份）."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    result = templates_mod.export_template(db_path, config, args.name, args.file)
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '导出失败')}")
        return 1
    print(f"[OK] 模板 {args.name} 已导出")
    print(f"  文件: {result['file_path']}")
    return 0


def cmd_template_run(args) -> int:
    """按模板批量执行 list/export/replay（支持续跑）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    template = templates_mod.get_template(db_path, config, name)
    if not template:
        print(f"[ERROR] 模板不存在：{name}")
        return 1

    output_dir = cfg.get_export_dir(config)
    allowed = cfg.get_allowed_statuses(config)
    operator = config.get("operator", "cli")

    execution_id = getattr(args, "execution_id", None)
    resume = getattr(args, "resume", False)

    if resume and execution_id is None:
        execs = db.list_executions(db_path, template_id=template["id"])
        pending = [e for e in execs if e.get("status") != "completed"]
        if not pending:
            print(f"[ERROR] 模板 {name} 没有可续跑的执行记录")
            return 1
        execution_id = pending[0]["id"]
        print(f"[INFO] 续跑执行记录 #{execution_id}")

    result = batch_mod.run_template(
        db_path, config, template, output_dir,
        allowed_statuses=allowed, operator=operator,
        execution_id=execution_id,
    )

    if not result.get("success") and result.get("error"):
        print(f"[ERROR] {result['error']}")
        return 1

    if result.get("version_drift"):
        print(f"[WARN] 版本漂移：{result['version_drift']['message']}")

    status = result["status"]
    eid = result["execution_id"]
    status_label = {
        "completed": "[OK] 完成",
        "failed": "[FAILED] 执行中断",
        "running": "[RUNNING] 执行中",
        "pending": "[PENDING] 待执行",
    }.get(status, f"[{status.upper()}]")
    print(f"{status_label} 执行 #{eid}："
          f"完成 {result['steps_done']}/{result['steps_total']}，"
          f"失败 {result['steps_failed']}")
    for s in result["steps"]:
        step = s["step"]
        label = step.get("action") + (f"/{step.get('type')}" if step.get("type") else "")
        if s["status"] == "skipped_done":
            r = s.get("result", {})
            extra = f" -> {os.path.basename(r.get('file_path'))}" if r.get("file_path") else ""
            print(f"  #{s['step_index']} {label}: 跳过（已完成，未重复执行）{extra}")
        elif s["status"] == "done":
            r = s.get("result", {})
            extra = f" -> {r.get('file_path')}" if r.get("file_path") else ""
            if r.get("count") is not None:
                extra += f" ({r['count']} 条)"
            print(f"  #{s['step_index']} {label}: 完成{extra}")
        else:
            print(f"  #{s['step_index']} {label}: 失败 - {s.get('error')}")

    if result.get("archive_path"):
        print(f"\n[归档] 执行清单已导出: {result['archive_path']}")

    print()
    print("[后续操作]")
    print(f"  查看执行详情: template-show {name}")
    print(f"  导出执行归档: template-export-execution {name}")
    if status != "completed":
        print(f"  续跑未完成步骤: template-run {name} --resume")
        print(f"\n[续跑条件] 当前状态为「{status}」，非 completed 状态均可续跑；"
              f"已完成步骤会标记为 skipped_done，不会重复产生日志或导出文件。")
        return 2
    else:
        print(f"  重新执行（新建独立执行记录）: template-run {name}")
        return 0


def cmd_template_export_execution(args) -> int:
    """导出执行归档清单（含模板快照、步骤结果、operator、激活方案）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    execution_id = getattr(args, "execution_id", None)
    if execution_id is None:
        name = getattr(args, "name", None)
        if not name:
            print("[ERROR] 请指定 --execution-id 或模板名称")
            return 1
        template = templates_mod.get_template(db_path, config, name)
        if not template:
            print(f"[ERROR] 模板不存在：{name}")
            return 1
        execs = db.list_executions(db_path, template_id=template["id"])
        if not execs:
            print(f"[ERROR] 模板 {name} 没有执行记录")
            return 1
        execution_id = execs[0]["id"]
        print(f"[INFO] 选择最近执行记录 #{execution_id}")

    output = getattr(args, "output", None)
    result = archive_mod.export_execution_manifest(
        db_path, config, execution_id, output_path=output,
    )
    if not result.get("success"):
        print(f"[ERROR] {result.get('error')}")
        return 1
    print(f"[OK] 执行归档已导出")
    print(f"  文件: {result['file_path']}")
    manifest = result.get("manifest", {})
    meta = manifest.get("execution_meta", {})
    print(f"  执行 #{meta.get('execution_id')}: {meta.get('template_name')} "
          f"v{meta.get('template_version')} {meta.get('status')}")
    print(f"  步骤: 完成 {meta.get('steps_done')}/{meta.get('steps_total')} "
          f"失败 {meta.get('steps_failed')}")
    print(f"  操作人: {meta.get('operator') or '-'}，激活方案: {meta.get('active_plan') or '-'}")
    export_count = len(manifest.get("export_files", []))
    if export_count:
        print(f"  相关导出文件: {export_count} 个")
        for ef in manifest.get("export_files", [])[:3]:
            print(f"    - {ef.get('filename')}")
        if export_count > 3:
            print(f"    ... 还有 {export_count - 3} 个")
    print()
    print("[后续操作]")
    print(f"  预览归档内容: template-preview-archive {os.path.basename(result['file_path'])}")
    print(f"  从归档恢复: template-restore-execution {os.path.basename(result['file_path'])}")
    print(f"  查看模板详情: template-show {meta.get('template_name')}")
    return 0


def cmd_template_preview_archive(args) -> int:
    """预检/预览归档清单（不执行恢复，先看清内容和冲突）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    manifest_file = args.file
    check_conflicts = not getattr(args, "no_conflict_check", False)

    if check_conflicts:
        result = archive_mod.preview_manifest(
            manifest_file, db_path=db_path, config=config,
        )
    else:
        result = archive_mod.preview_manifest(manifest_file)

    if not result.get("success"):
        print(f"[ERROR] {result.get('error')}")
        return 1

    p = result["preview"]
    exec_info = p["execution"]
    tpl_info = p["template"]

    print("=" * 70)
    print("=== 归档清单预览 ===")
    print("=" * 70)
    print(f"文件: {p['manifest_file']}")
    print(f"导出版本: v{p['manifest_version']}，导出时间: {p['exported_at']}")
    print()

    print("--- 执行信息 ---")
    print(f"  原执行 ID: #{exec_info['id']}")
    print(f"  状态: {exec_info['status']}")
    print(f"  开始: {exec_info['started_at']}")
    if exec_info['finished_at']:
        print(f"  结束: {exec_info['finished_at']}")
    print(f"  步骤: 完成 {exec_info['steps_done']}/{exec_info['steps_total']}，"
          f"失败 {exec_info['steps_failed']}")
    print(f"  操作人: {p['operator'] or '-'}")
    print(f"  激活方案: {p['active_plan'] or '-'}")
    print()

    print("--- 模板快照 ---")
    print(f"  名称: {tpl_info['name']}")
    print(f"  版本: v{tpl_info['version']}")
    if tpl_info.get('description'):
        print(f"  描述: {tpl_info['description']}")
    print(f"  内容指纹: {tpl_info['content_hash']}")
    f = tpl_info["filters"]
    print(f"  筛选: status={f['status'] or '-'}, "
          f"location={f['location'] or '-'}, sku={f['sku'] or '-'}")
    if tpl_info.get('export_fields'):
        print(f"  导出字段: {', '.join(tpl_info['export_fields'])}")
    if tpl_info.get('remark_template'):
        print(f"  备注模板: {tpl_info['remark_template']}")
    print()

    print("--- 执行步骤 ---")
    for s in p["steps"]:
        label = s["action"] + (f"/{s['detail']}" if s['detail'] else "")
        status_label = {
            "done": "[完成]",
            "failed": "[失败/中断]",
            "skipped_done": "[跳过/已完成]",
            "pending": "[待执行]",
            "running": "[运行中]",
        }.get(s["status"], f"[{s['status']}]")
        extra = s.get("extra", "")
        error = s.get("error")
        print(f"  #{s['index']:2d} {status_label} {label}{extra}")
        if error:
            print(f"       错误: {error}")
    print()

    exec_status = exec_info.get("status", "")
    resumable = exec_status != "completed"
    status_cn = {
        "running": "执行中（可续跑）",
        "failed": "执行中断（可续跑）",
        "pending": "待执行（可续跑）",
        "completed": "已完成（无需续跑）",
    }.get(exec_status, exec_status)
    print(f"[执行状态] {exec_status} — {status_cn}")
    if resumable:
        print("  → 非 completed 状态均可续跑，续跑时已完成步骤标记为 skipped_done，不重复导出")
    else:
        print("  → 已全部完成；如需重新执行，请新建执行记录")
    print()

    if p["export_files"]:
        print("--- 导出文件 ---")
        for ef in p["export_files"]:
            size_note = ""
            if ef.get("file_size") is not None:
                size_note = f" ({ef['file_size']} bytes)"
            exists_note = "" if ef.get("file_exists") else " (文件已丢失)"
            print(f"  步骤#{ef['step_index']} [{ef['type']}] "
                  f"{ef['filename']}{size_note}{exists_note}")
        print()

    print(f"相关操作日志: {p['operation_logs_count']} 条")
    print()

    if check_conflicts and result.get("conflicts"):
        print("--- 恢复冲突检测 ---")
        blocking = [c for c in result["conflicts"]
                    if c["type"] in archive_mod.BLOCKING_CONFLICT_TYPES]
        non_blocking = [c for c in result["conflicts"]
                        if c["type"] not in archive_mod.BLOCKING_CONFLICT_TYPES]
        if blocking:
            print(f"[阻塞冲突] 共 {len(blocking)} 项，需处理后才能恢复：")
            for c in blocking:
                print(f"  ! [{c['type']}] {c['message']}")
                if c.get("resolution"):
                    print(f"      → save-as 处理方式: {c['resolution']}")
        if non_blocking:
            print(f"[提示冲突] 共 {len(non_blocking)} 项，不阻塞恢复：")
            for c in non_blocking:
                print(f"  - [{c['type']}] {c['message']}")
                if c.get("resolution"):
                    print(f"      → 处理方式: {c['resolution']}")
        print()

    print(f"[建议] {result.get('suggestion', '')}")
    print("=" * 70)
    return 0


def cmd_template_restore_execution(args) -> int:
    """从归档清单恢复执行历史（支持冲突检测与处理）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    manifest_file = args.file
    resolution = getattr(args, "conflict", "abort")

    print(f"[INFO] 正在加载归档: {os.path.basename(manifest_file)}")
    preview = archive_mod.preview_manifest(
        manifest_file, db_path=db_path, config=config,
    )
    if not preview.get("success"):
        print(f"[ERROR] {preview.get('error')}")
        return 1

    p = preview["preview"]
    exec_info = p["execution"]
    tpl_info = p["template"]
    print(f"[INFO] 原执行 #{exec_info['id']}: {tpl_info['name']} v{tpl_info['version']} "
          f"({exec_info['status']}, 完成 {exec_info['steps_done']}/{exec_info['steps_total']})")

    if preview.get("conflicts"):
        blocking = [c for c in preview["conflicts"]
                    if c["type"] in archive_mod.BLOCKING_CONFLICT_TYPES]
        if blocking:
            print(f"[WARN] 检测到 {len(blocking)} 项阻塞冲突:")
            for c in blocking[:5]:
                print(f"  - [{c['type']}] {c['message']}")
            if len(blocking) > 5:
                print(f"  ... 还有 {len(blocking) - 5} 项")
            if resolution == "abort":
                print("[INFO] 使用 --conflict save-as 可自动处理这些冲突")

    result = archive_mod.restore_execution_from_manifest(
        db_path, config, manifest_file, conflict_resolution=resolution,
    )

    if not result.get("success"):
        if result.get("conflicts"):
            print("[冲突] 恢复中止，检测到以下阻塞冲突：")
            for c in result["conflicts"]:
                severity = "!" if c.get("severity") == "error" else "-"
                print(f"  {severity} [{c.get('type')}] {c.get('message')}")
                if c.get("resolution"):
                    print(f"      → save-as 处理方式: {c['resolution']}")
            print()
            print("  可用冲突处理策略：")
            print("    --conflict abort    : 中止（默认），不改动任何数据")
            print("    --conflict save-as  : 自动处理上述所有冲突 - 模板另存为新名、")
            print("                          保留现有文件、按归档记录恢复方案")
        err = result.get("error") or "恢复失败"
        if result.get("template_conflict"):
            print(f"[冲突] {err}")
            print("  使用 --conflict save-as 另存为新模板名后恢复")
        else:
            print(f"[ERROR] {err}")
        return 1

    if result.get("conflicts"):
        non_blocking = [c for c in result["conflicts"]
                        if c["type"] not in archive_mod.BLOCKING_CONFLICT_TYPES]
        if non_blocking:
            print("[提示] 恢复时检测到非阻塞冲突：")
            for c in non_blocking:
                print(f"  - [{c.get('type')}] {c.get('message')}")

    tpl = result.get("template") or {}
    print(f"[OK] 执行历史已恢复")
    print(f"  新执行 ID: #{result['execution_id']} "
          f"(原归档 #{result.get('original_execution_id')})")
    action = result.get("template_action", "")
    name_note = ""
    if action == "save_as":
        name_note = f" (另存为 {result.get('name')})"
    print(f"  模板: {tpl.get('name')} v{tpl.get('version')}{name_note}")
    print(f"  步骤数: {result.get('steps_restored', 0)}")
    print(f"  操作人: {config.get('operator', 'cli')}")
    if result.get("logs_restored", 0) > 0:
        print(f"  操作日志恢复: {result['logs_restored']} 条")

    exec_status = exec_info.get("status", "")
    resumable = exec_status != "completed"
    if resumable:
        print()
        status_cn = {
            "running": "执行中",
            "failed": "执行中断",
            "pending": "待执行",
        }.get(exec_status, exec_status)
        print(f"[续跑条件] 原执行状态为「{exec_status}（{status_cn}）」，"
              f"非 completed 状态均可续跑。已完成步骤在续跑时会标记为 skipped_done，"
              f"不会重复产生日志或导出文件。")
        print(f"  续跑命令: template-run {tpl.get('name')} --resume")
    else:
        print()
        print(f"[状态] 原执行状态为「completed（已完成）」，无需续跑。"
              f"如需重新执行，请新建执行记录。")
    print()
    print("[验证操作] 恢复后可执行以下操作确认状态一致：")
    print(f"  template-show {tpl.get('name')}              # 查看模板和执行记录（状态/步骤统计）")
    if resumable:
        print(f"  template-run {tpl.get('name')} --resume      # 续跑未完成步骤")
    else:
        print(f"  template-run {tpl.get('name')}               # 重新执行（新建独立执行记录）")
    print(f"  template-export-execution {tpl.get('name')}  # 再次导出归档，核对元数据")
    return 0


# ============================================================================
# 审计会话归档包 (session-archive-*)
# ============================================================================

def cmd_session_archive_create(args) -> int:
    """创建审计会话归档包 - 打包数据库/配置/导出报表/操作日志为可搬走的 zip."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    output = getattr(args, "output", None)
    operator = config.get("operator", "cli")

    result = session_archive_mod.create_session_archive(
        db_path, config,
        config_file_path=getattr(args, "config", None),
        output_path=output,
        operator=operator,
    )

    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '归档失败')}")
        return 1

    summary = result["summary"]
    db_info = summary["database"]
    print(f"[OK] 审计会话归档已创建")
    print(f"  归档文件: {result['archive_path']}")
    print(f"  归档版本: v{result['manifest']['$archive_version']}，工具版本: {result['manifest']['tool_version']}")
    print(f"  操作人: {result['manifest']['operator']}，激活方案: {result['manifest'].get('active_plan') or '(无)'}")
    print(f"  数据库: {db_info['size_bytes']} 字节")
    rc = db_info.get("row_counts", {})
    print(f"  行数摘要: 批次 {rc.get('batches', 0)} / 差异 {rc.get('differences', 0)} / "
          f"来源行 {rc.get('source_lines', 0)} / 操作日志 {rc.get('operation_logs', 0)}")
    print(f"  导出报表: {len(summary.get('exports', []))} 个")
    print(f"  方案/模板: plans={len(summary.get('plans', []))}, "
          f"templates={len(summary.get('templates', []))}, "
          f"batch_templates={len(summary.get('batch_templates', []))}")
    print(f"  归档总文件数: {summary.get('total_files', 0)}")
    print()
    print("[后续操作]")
    print(f"  查看归档内容: session-archive-info {os.path.basename(result['archive_path'])}")
    print(f"  恢复到新目录: session-archive-restore {os.path.basename(result['archive_path'])} "
          f"--target-dir <目标目录>")
    return 0


def cmd_session_archive_info(args) -> int:
    """列出归档内容摘要 - 不恢复，先看清归档里带了什么."""
    archive = args.archive
    result = session_archive_mod.list_archive_contents(archive)
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '读取归档失败')}")
        return 1

    manifest = result["manifest"]
    summary = result["summary"]
    db_info = summary.get("database", {})

    print("=" * 70)
    print("=== 审计会话归档内容摘要 ===")
    print("=" * 70)
    print(f"归档文件: {result['archive_path']}")
    print(f"归档版本: v{manifest.get('$archive_version')}（当前工具支持 "
          f"{', '.join('v' + str(v) for v in session_archive_mod.SUPPORTED_ARCHIVE_VERSIONS)}）")
    print(f"工具版本: {manifest.get('tool_version')}")
    print(f"创建时间: {manifest.get('created_at')}")
    print(f"操作人: {manifest.get('operator') or '-'}，激活方案: {manifest.get('active_plan') or '(无)'}")
    src = manifest.get("source", {})
    print(f"来源: 数据库 {src.get('db_path') or '-'}")
    if src.get("config_file"):
        print(f"      配置文件 {src.get('config_file')}")
    print()

    print("--- 数据库 ---")
    print(f"  归档路径: {db_info.get('archive_path')}")
    print(f"  大小: {db_info.get('size_bytes', 0)} 字节")
    rc = db_info.get("row_counts", {})
    if rc:
        print(f"  行数: " + ", ".join(f"{k}={v}" for k, v in rc.items()))
    print()

    exports = summary.get("exports", [])
    print(f"--- 导出报表 ({len(exports)} 个) ---")
    for ef in exports[:5]:
        print(f"  {ef.get('filename')} ({ef.get('size_bytes', 0)} 字节)")
    if len(exports) > 5:
        print(f"  ... 还有 {len(exports) - 5} 个")
    print()

    plans = summary.get("plans", [])
    templates = summary.get("templates", [])
    batch_templates = summary.get("batch_templates", [])
    print(f"--- 方案/模板 ---")
    print(f"  plans ({len(plans)}): {', '.join(plans) if plans else '(无)'}")
    print(f"  templates ({len(templates)}): {', '.join(templates) if templates else '(无)'}")
    print(f"  batch_templates ({len(batch_templates)}): {', '.join(batch_templates) if batch_templates else '(无)'}")
    print(f"  runtime_state: {'已包含' if summary.get('runtime_state_present') else '未包含'}")
    print(f"  operation_logs: {summary.get('operation_logs_count', 0)} 条（DB 内 + data/operation_logs.json 留档）")
    print(f"  归档总文件数: {summary.get('total_files', 0)}")
    print("=" * 70)
    return 0


def cmd_session_archive_restore(args) -> int:
    """从归档恢复到新工作目录 - 支持 --conflict abort|rename|overwrite."""
    archive = args.archive
    target_dir = getattr(args, "target_dir", None)
    conflict = getattr(args, "conflict", "abort")
    operator = None

    # 先加载配置以取得操作人记录到恢复日志
    config = _load_config(args)
    operator = config.get("operator", "cli")

    if target_dir is None:
        print("[ERROR] 恢复必须指定 --target-dir 目标工作目录")
        return 1

    # 恢复前先预检，给用户看清冲突
    preview = session_archive_mod.read_archive_manifest(archive)
    if not preview.get("success"):
        print(f"[ERROR] {preview.get('error', '归档读取失败')}")
        return 1
    manifest = preview["manifest"]
    print(f"[INFO] 正在恢复归档: {os.path.basename(archive)}")
    print(f"  归档版本 v{manifest.get('$archive_version')}，创建于 {manifest.get('created_at')}")
    print(f"  来源操作人: {manifest.get('operator') or '-'}，激活方案: {manifest.get('active_plan') or '(无)'}")

    conflicts = session_archive_mod.detect_restore_conflicts(archive, target_dir)
    if conflicts:
        print(f"[WARN] 检测到 {len(conflicts)} 项目标冲突:")
        for c in conflicts:
            print(f"  - [{c['type']}] {c['message']}")
            print(f"      → {c['resolution']}")
        if conflict == "abort":
            print("[INFO] 当前策略为 abort，已中止（未改动任何数据）。")
            print("  使用 --conflict rename 另存，或 --conflict overwrite 覆盖后再恢复。")

    result = session_archive_mod.restore_session_archive(
        archive, target_dir, conflict=conflict, operator=operator,
    )

    if not result.get("success"):
        if result.get("conflict"):
            print("[冲突] 恢复中止，未改动任何数据：")
            for c in result["conflicts"]:
                print(f"  ! [{c['type']}] {c['message']}")
            print()
            print("  可用冲突处理策略：")
            print("    --conflict abort     : 中止（默认），不改动任何数据")
            print("    --conflict rename    : 恢复到 audit_data_restored/ 与 config_restored.json，保留现有数据")
            print("    --conflict overwrite : 直接覆盖已有数据库与配置文件")
            return 2
        else:
            print(f"[ERROR] {result.get('error', '恢复失败')}")
        return 1

    if result.get("renamed"):
        print(f"[INFO] 检测到冲突，已按 rename 策略另存：")
        print(f"  数据目录: {result['audit_data_dir']}（原 audit_data 保留不动）")
        print(f"  配置文件: {result['config_path']}（原 config.json 保留不动）")
    print(f"[OK] 审计会话已恢复到: {result['target_root']}")
    print(f"  数据库: {result['db_path']}")
    print(f"  配置文件: {result['config_path']}")
    print(f"  恢复文件数: {result['restored_files']}")
    rc = result.get("row_counts", {})
    if rc:
        print(f"  恢复后行数: 批次 {rc.get('batches', 0)} / 差异 {rc.get('differences', 0)} / "
              f"操作日志 {rc.get('operation_logs', 0)}")
    if result.get("logged"):
        print(f"  恢复动作已写入操作日志 (session_restore)")
    print()
    launcher = _launcher_path()
    print("[后续操作] 在新工作目录继续工作（配置中的相对路径以 config.json 所在目录为基准）：")
    print(f"  cd {result['target_root']}")
    print(f"  python {launcher} -c config.json list")
    print(f"  python {launcher} -c config.json show 1")
    print(f"  python {launcher} -c config.json export -t differences")
    print(f"  python {launcher} -c config.json undo")
    print("  （启动器无需 PYTHONPATH；在源码树内也可继续用 python -m inventory_audit ...）")
    return 0


# ============================================================================
# 批量任务模板 (batch-template-*)
# ============================================================================

def _parse_json_arg(s: Optional[str]) -> Optional[Dict[str, Any]]:
    """解析 JSON 字符串为 dict，失败返回 None 并打印错误."""
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON 解析失败: {e}")
        return None


def cmd_bt_save(args) -> int:
    """保存（新增或修改）批量任务模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    description = getattr(args, "description", None)

    execution_params = _parse_json_arg(getattr(args, "params", None))
    if getattr(args, "params", None) is not None and execution_params is None:
        return 1
    if not isinstance(execution_params, dict):
        execution_params = None

    env_whitelist = getattr(args, "env", None)
    if env_whitelist:
        env_whitelist = [e.strip() for e in env_whitelist.split(",") if e.strip()]

    export_options = _parse_json_arg(getattr(args, "export_opts", None))
    if getattr(args, "export_opts", None) is not None and export_options is None:
        return 1
    if not isinstance(export_options, dict):
        export_options = None

    conflict_strategy = getattr(args, "conflict_default", "abort")
    disabled = getattr(args, "disabled", False)
    operator = config.get("operator", "cli")

    result = bt_mod.save_batch_template(
        db_path, config, name,
        description=description,
        execution_params=execution_params,
        env_whitelist=env_whitelist,
        export_options=export_options,
        conflict_strategy=conflict_strategy,
        disabled=disabled,
        operator=operator,
    )

    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '保存失败')}")
        return 1

    action = result.get("action", "create")
    action_cn = {"create": "已创建", "modify": "已修改", "unchanged": "无变更"}.get(action, action)
    print(f"[OK] 批量任务模板 {name} {action_cn}（ID={result['template_id']}）")
    tpl = bt_mod.get_batch_template(db_path, config, name)
    if tpl:
        if tpl.get("description"):
            print(f"  描述: {tpl['description']}")
        if tpl.get("execution_params"):
            print(f"  执行参数: {json.dumps(tpl['execution_params'], ensure_ascii=False)}")
        if tpl.get("env_whitelist"):
            print(f"  环境变量白名单: {', '.join(tpl['env_whitelist'])}")
        if tpl.get("export_options"):
            print(f"  导出选项: {json.dumps(tpl['export_options'], ensure_ascii=False)}")
        print(f"  默认冲突策略: {tpl.get('conflict_strategy', 'abort')}")
        print(f"  状态: {'禁用' if tpl.get('disabled') else '启用'}")
    return 0


def cmd_bt_list(args) -> int:
    """列出批量任务模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    include_disabled = not getattr(args, "all", True)

    templates = bt_mod.list_batch_templates(db_path, include_disabled=include_disabled)
    if not templates:
        print("暂无批量任务模板")
        return 0

    print(f"共 {len(templates)} 个批量任务模板:")
    print("-" * 80)
    print(f"{'ID':<4} {'名称':<24} {'状态':<6} {'冲突策略':<10} {'更新时间':<20}")
    print("-" * 80)
    for t in templates:
        status = "禁用" if t.get("disabled") else "启用"
        print(f"{t['id']:<4} {t['name']:<24} {status:<6} "
              f"{t.get('conflict_strategy', 'abort'):<10} {t.get('updated_at', ''):<20}")
    return 0


def cmd_bt_show(args) -> int:
    """查看批量任务模板详情."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    tpl = bt_mod.get_batch_template(db_path, config, name)
    if not tpl:
        print(f"[ERROR] 批量任务模板不存在：{name}")
        return 1

    print(f"=== 批量任务模板: {tpl['name']} (ID={tpl['id']}) ===")
    if tpl.get("description"):
        print(f"描述: {tpl['description']}")
    print(f"状态: {'禁用' if tpl.get('disabled') else '启用'}")
    print(f"默认冲突策略: {tpl.get('conflict_strategy', 'abort')}")
    print(f"内容指纹: {tpl.get('content_hash', '')[:16]}...")
    if tpl.get("execution_params"):
        print(f"\n执行参数:")
        print(f"  {json.dumps(tpl['execution_params'], ensure_ascii=False, indent=2)}")
    if tpl.get("env_whitelist"):
        print(f"\n环境变量白名单 ({len(tpl['env_whitelist'])} 项):")
        for e in tpl["env_whitelist"]:
            print(f"  - {e}")
    if tpl.get("export_options"):
        print(f"\n导出选项:")
        print(f"  {json.dumps(tpl['export_options'], ensure_ascii=False, indent=2)}")
    print(f"\n创建时间: {tpl.get('created_at', '')}")
    print(f"更新时间: {tpl.get('updated_at', '')}")
    return 0


def cmd_bt_delete(args) -> int:
    """删除批量任务模板（可 undo）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    operator = config.get("operator", "cli")
    result = bt_mod.delete_batch_template(db_path, config, name, operator=operator)
    if not result.get("success"):
        print(f"[WARN] {result.get('error', '删除失败')}")
        return 0
    print(f"[OK] {result['message']}")
    if result.get("action") == "deleted":
        print(f"  ID: {result['template_id']}")
        print(f"  [提示] 可用 batch-template-undo {name} 撤销此次删除")
    return 0


def cmd_bt_disable(args) -> int:
    """禁用批量任务模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    operator = config.get("operator", "cli")
    result = bt_mod.set_batch_template_disabled(
        db_path, config, name, disabled=True, operator=operator,
    )
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '禁用失败')}")
        return 1
    print(f"[OK] {result['message']}")
    if result.get("action") != "unchanged":
        print(f"  [提示] 可用 batch-template-undo {name} 撤销此次禁用")
    return 0


def cmd_bt_enable(args) -> int:
    """启用批量任务模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    operator = config.get("operator", "cli")
    result = bt_mod.set_batch_template_disabled(
        db_path, config, name, disabled=False, operator=operator,
    )
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '启用失败')}")
        return 1
    print(f"[OK] {result['message']}")
    if result.get("action") != "unchanged":
        print(f"  [提示] 可用 batch-template-undo {name} 撤销此次启用")
    return 0


def cmd_bt_copy(args) -> int:
    """复制批量任务模板."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    src_name = args.source
    dst_name = args.destination
    operator = config.get("operator", "cli")

    result = bt_mod.copy_batch_template(
        db_path, config, src_name, dst_name, operator=operator,
    )
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '复制失败')}")
        return 1

    print(f"[OK] {result['message']}")
    return 0


def cmd_bt_undo(args) -> int:
    """撤销批量任务模板的最近一次变更."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    operator = config.get("operator", "cli")
    result = bt_mod.undo_last_batch_template_change(
        db_path, config, name, operator=operator,
    )
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '撤销失败')}")
        return 1
    print(f"[OK] {result['message']}")
    return 0


def cmd_bt_export(args) -> int:
    """导出批量任务模板为 JSON 文件."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    name = args.name
    file_path = args.file
    result = bt_mod.export_batch_template(db_path, config, name, file_path)
    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '导出失败')}")
        return 1
    print(f"[OK] 批量任务模板 {name} 已导出")
    print(f"  文件: {result['file_path']}")
    return 0


def cmd_bt_import(args) -> int:
    """从 JSON 文件导入批量任务模板（带冲突策略）."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    file_path = args.file
    conflict = getattr(args, "conflict", "abort")
    operator = config.get("operator", "cli")

    result = bt_mod.import_batch_template(
        db_path, config, file_path, conflict=conflict, operator=operator,
    )

    if not result.get("success"):
        if result.get("conflict"):
            print(f"[冲突] {result['message']}")
            return 2
        print(f"[ERROR] {result.get('error', '导入失败')}")
        return 1

    action = result.get("action", "create")
    action_cn = {
        "create": "已导入（新建）",
        "modify": "已覆盖更新",
        "unchanged": "内容未变化，跳过",
    }.get(action, action)
    print(f"[OK] {result['message']}")
    print(f"  操作: {action_cn}")
    print(f"  模板名: {result.get('saved_as') or result.get('name') or args.file}")
    if result.get("imported_from"):
        print(f"  来源: {result['imported_from']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器."""
    parser = argparse.ArgumentParser(
        prog="inventory-audit",
        description="仓库盘点差异复核 CLI 工具",
    )
    parser.add_argument(
        "-c", "--config",
        help="配置文件路径 (JSON 格式)",
        default=None,
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_init = subparsers.add_parser("init", help="初始化数据库和目录")
    p_init.set_defaults(func=cmd_init)

    p_import = subparsers.add_parser("import", help="导入盘点 CSV")
    p_import.add_argument("csv_file", help="CSV 文件路径")
    p_import.add_argument("-n", "--name", help="批次名称", default=None)
    p_import.set_defaults(func=cmd_import)

    p_list = subparsers.add_parser("list", help="列出差异（支持方案筛选）")
    p_list.add_argument("-s", "--status", help="按状态过滤（覆盖方案）", default=None)
    p_list.add_argument("-l", "--location", help="按库位过滤（覆盖方案）", default=None)
    p_list.add_argument("--sku", help="按 SKU 过滤（覆盖方案）", default=None)
    p_list.set_defaults(func=cmd_list)

    p_show = subparsers.add_parser("show", help="查看差异详情")
    p_show.add_argument("diff_id", type=int, help="差异 ID")
    p_show.set_defaults(func=cmd_show)

    p_status = subparsers.add_parser("status", help="设置差异状态（记录当前方案）")
    p_status.add_argument("diff_ids", type=int, nargs="+", help="差异 ID（可多个）")
    p_status.add_argument("status", help="状态值 (由配置 status.allowed 决定合法值)")
    p_status.set_defaults(func=cmd_status)

    p_remark = subparsers.add_parser("remark", help="设置差异备注（支持方案备注模板）")
    p_remark.add_argument("diff_id", type=int, help="差异 ID")
    p_remark.add_argument("remark", help="备注内容")
    p_remark.set_defaults(func=cmd_remark)

    p_undo = subparsers.add_parser("undo", help="撤销最后一次复核操作")
    p_undo.set_defaults(func=cmd_undo)

    p_history = subparsers.add_parser("history", help="查看复核历史")
    p_history.add_argument("-d", "--diff-id", type=int, help="指定差异 ID", default=None)
    p_history.add_argument("-n", "--limit", type=int, help="显示条数", default=20)
    p_history.set_defaults(func=cmd_history)

    p_export = subparsers.add_parser("export", help="导出报告（支持方案字段）")
    p_export.add_argument(
        "-t", "--type",
        choices=["differences", "summary", "sources"],
        default="differences",
        help="导出类型: differences(差异明细), summary(汇总), sources(来源行)",
    )
    p_export.add_argument("-s", "--status", help="按状态过滤（覆盖方案）", default=None)
    p_export.add_argument("-b", "--batch-id", type=int, help="批次 ID (sources 类型)", default=None)
    p_export.set_defaults(func=cmd_export)

    p_batches = subparsers.add_parser("batches", help="查看批次列表")
    p_batches.set_defaults(func=cmd_batches)

    p_summary = subparsers.add_parser("summary", help="查看汇总统计")
    p_summary.set_defaults(func=cmd_summary)

    p_remerge = subparsers.add_parser("remerge", help="重新合并差异（数据修复用）")
    p_remerge.add_argument("-f", "--force", action="store_true", help="跳过确认")
    p_remerge.set_defaults(func=cmd_remerge)

    p_operator = subparsers.add_parser("set-operator", help="设置操作人（重启后续用）")
    p_operator.add_argument("operator", help="操作人名称")
    p_operator.set_defaults(func=cmd_set_operator)

    p_plan_save = subparsers.add_parser("plan-save", help="保存复核方案（筛选/导出字段/备注模板）")
    p_plan_save.add_argument("name", help="方案名称（唯一）")
    p_plan_save.add_argument("-s", "--status", help="状态过滤", default=None)
    p_plan_save.add_argument("-l", "--location", help="库位过滤", default=None)
    p_plan_save.add_argument("--sku", help="SKU 过滤", default=None)
    p_plan_save.add_argument(
        "-f", "--fields",
        help="导出字段（逗号分隔，如 id,location,sku,total_diff_qty,status,remark）",
        default=None,
    )
    p_plan_save.add_argument("-r", "--remark-template", help="备注模板", default=None)
    p_plan_save.set_defaults(func=cmd_plan_save)

    p_plan_list = subparsers.add_parser("plan-list", help="列出所有复核方案")
    p_plan_list.set_defaults(func=cmd_plan_list)

    p_plan_use = subparsers.add_parser("plan-use", help="激活方案（重启后续用），不带参数则清除")
    p_plan_use.add_argument("name", nargs="?", help="方案名称")
    p_plan_use.set_defaults(func=cmd_plan_use)

    p_plan_delete = subparsers.add_parser("plan-delete", help="删除复核方案")
    p_plan_delete.add_argument("name", help="方案名称")
    p_plan_delete.set_defaults(func=cmd_plan_delete)

    p_replay = subparsers.add_parser("replay", help="按操作日志回放（含冲突检测）")
    p_replay.add_argument("-p", "--plan", help="按方案过滤回放日志", default=None)
    p_replay.add_argument("-o", "--operator", help="按操作人过滤回放日志", default=None)
    p_replay.add_argument(
        "-r", "--resolution",
        choices=["keep", "snapshot", "abort"],
        default="abort",
        help="冲突处理策略: keep(保留当前), snapshot(另存快照后跳过), abort(中止,默认)",
    )
    p_replay.add_argument(
        "-a", "--action-types",
        default=None,
        help="只回放指定动作类型（逗号分隔）, 如: status_change,remark_change,export,undo",
    )
    p_replay.set_defaults(func=cmd_replay)

    # --- 模板（复核方案模板 + 批量执行） ---
    p_tpl_save = subparsers.add_parser(
        "template-save", help="保存复核方案模板（筛选/导出字段/回放动作，版本化）",
    )
    p_tpl_save.add_argument("name", help="模板名称（唯一）")
    p_tpl_save.add_argument("-s", "--status", help="状态过滤", default=None)
    p_tpl_save.add_argument("-l", "--location", help="库位过滤", default=None)
    p_tpl_save.add_argument("--sku", help="SKU 过滤", default=None)
    p_tpl_save.add_argument(
        "-f", "--fields", help="导出字段（逗号分隔）", default=None,
    )
    p_tpl_save.add_argument("-r", "--remark-template", help="备注模板", default=None)
    p_tpl_save.add_argument("-d", "--description", help="模板描述", default=None)
    p_tpl_save.add_argument(
        "--steps", help="步骤简写（逗号分隔: list,export,export:summary,replay）",
        default=None,
    )
    p_tpl_save.add_argument(
        "--steps-file", help="步骤定义文件(JSON 数组，优先于 --steps)", default=None,
    )
    p_tpl_save.add_argument(
        "--force", action="store_true",
        help="内容冲突时强制覆盖（bump 版本，已有执行记录保留旧版本）",
    )
    p_tpl_save.set_defaults(func=cmd_template_save)

    p_tpl_list = subparsers.add_parser("template-list", help="列出所有模板")
    p_tpl_list.set_defaults(func=cmd_template_list)

    p_tpl_show = subparsers.add_parser("template-show", help="查看模板详情")
    p_tpl_show.add_argument("name", help="模板名称")
    p_tpl_show.set_defaults(func=cmd_template_show)

    p_tpl_del = subparsers.add_parser("template-delete", help="删除模板")
    p_tpl_del.add_argument("name", help="模板名称")
    p_tpl_del.set_defaults(func=cmd_template_delete)

    p_tpl_import = subparsers.add_parser(
        "template-import", help="从 JSON 文件导入模板（处理重名/冲突）",
    )
    p_tpl_import.add_argument("file", help="模板配置文件路径(JSON)")
    p_tpl_import.add_argument(
        "--force", action="store_true", help="内容冲突时强制覆盖",
    )
    p_tpl_import.set_defaults(func=cmd_template_import)

    p_tpl_export = subparsers.add_parser(
        "template-export", help="把模板导出为 JSON 配置文件",
    )
    p_tpl_export.add_argument("name", help="模板名称")
    p_tpl_export.add_argument("file", help="输出文件路径(JSON)")
    p_tpl_export.set_defaults(func=cmd_template_export)

    p_tpl_run = subparsers.add_parser(
        "template-run",
        help="按模板批量执行（支持续跑）。执行状态：completed/failed/running/pending，非 completed 均可续跑",
    )
    p_tpl_run.add_argument("name", help="模板名称")
    p_tpl_run.add_argument(
        "--execution-id", type=int, default=None,
        help="续跑指定执行记录 ID；与该 ID 状态无关，只要不是 completed 就从失败/待执行步骤继续",
    )
    p_tpl_run.add_argument(
        "--resume", action="store_true",
        help="自动选择最近一条状态非 completed 的执行记录续跑；已完成步骤标记为 skipped_done，不重复导出",
    )
    p_tpl_run.set_defaults(func=cmd_template_run)

    p_tpl_export_exec = subparsers.add_parser(
        "template-export-execution",
        help="导出执行归档清单（含模板快照、步骤状态、导出文件、操作日志），用于跨环境恢复和续跑",
    )
    p_tpl_export_exec.add_argument(
        "name", nargs="?", default=None, help="模板名称（默认取最近一条执行）",
    )
    p_tpl_export_exec.add_argument(
        "-e", "--execution-id", type=int, default=None, help="指定执行记录 ID（优先于模板名）",
    )
    p_tpl_export_exec.add_argument(
        "-o", "--output", default=None, help="输出文件路径(JSON)，默认 archives/exec_{id}_{tpl}_{ts}.json",
    )
    p_tpl_export_exec.set_defaults(func=cmd_template_export_execution)

    p_tpl_preview_archive = subparsers.add_parser(
        "template-preview-archive",
        help="【恢复前必做】预检归档：查看执行状态、步骤、导出文件、冲突检测，不执行任何恢复",
    )
    p_tpl_preview_archive.add_argument("file", help="归档清单文件路径(JSON)")
    p_tpl_preview_archive.add_argument(
        "--no-conflict-check", action="store_true",
        help="仅离线预览内容，不连接数据库检测冲突（不验证模板升级/文件存在/方案一致）",
    )
    p_tpl_preview_archive.set_defaults(func=cmd_template_preview_archive)

    p_tpl_restore_exec = subparsers.add_parser(
        "template-restore-execution",
        help="从归档恢复执行历史 + 模板快照。恢复后 failed 状态可 --resume 续跑，completed 可重执行",
    )
    p_tpl_restore_exec.add_argument("file", help="归档清单文件路径(JSON)")
    p_tpl_restore_exec.add_argument(
        "--conflict", choices=["abort", "save-as"], default="abort",
        help=(
            "冲突处理策略："
            "abort(中止,默认) — 检测到任意阻塞冲突立即中止，不改动任何数据；"
            "save-as(自动处理) — 模板升级则另存为 <name>_restored，导出文件已存在则仅恢复元数据不覆盖磁盘，"
            "方案不一致则按归档记录恢复执行且不改动当前 runtime 激活方案"
        ),
    )
    p_tpl_restore_exec.set_defaults(func=cmd_template_restore_execution)

    # --- 审计会话归档包 (session-archive-*) ---
    p_sa_create = subparsers.add_parser(
        "session-archive-create",
        help="创建审计会话归档包：打包数据库/配置/导出报表/操作日志为可搬走的 zip",
    )
    p_sa_create.add_argument(
        "-o", "--output", default=None,
        help="自定义输出路径(.zip)；不传则自动生成到 <audit_data>/archives/session_<操作人>_<时间戳>.zip",
    )
    p_sa_create.set_defaults(func=cmd_session_archive_create)

    p_sa_info = subparsers.add_parser(
        "session-archive-info",
        help="列出归档内容摘要（不恢复）：数据库/导出报表/方案模板/操作日志统计",
    )
    p_sa_info.add_argument("archive", help="归档文件路径(.zip)")
    p_sa_info.set_defaults(func=cmd_session_archive_info)

    p_sa_restore = subparsers.add_parser(
        "session-archive-restore",
        help="从归档恢复到新工作目录，恢复后可继续 list/show/export/undo",
    )
    p_sa_restore.add_argument("archive", help="归档文件路径(.zip)")
    p_sa_restore.add_argument(
        "--target-dir", default=None,
        help="恢复目标工作目录（不存在会自动创建）",
    )
    p_sa_restore.add_argument(
        "--conflict",
        choices=["abort", "rename", "overwrite"],
        default="abort",
        help=(
            "冲突处理策略（遇到已有数据库或同名配置时）："
            "abort(中止,默认) — 不改动任何数据；"
            "rename(另存) — 恢复到 audit_data_restored/ 与 config_restored.json，保留现有数据；"
            "overwrite(覆盖) — 直接覆盖已有数据库与配置文件"
        ),
    )
    p_sa_restore.set_defaults(func=cmd_session_archive_restore)

    # --- 批量任务模板 (batch-template-*) ---
    p_bt_save = subparsers.add_parser(
        "batch-template-save",
        help="保存批量任务模板（执行参数、环境变量白名单、导出选项、冲突策略），可新增/修改",
    )
    p_bt_save.add_argument("name", help="模板名称（唯一）")
    p_bt_save.add_argument("-d", "--description", help="模板描述", default=None)
    p_bt_save.add_argument(
        "-p", "--params",
        help="执行参数（JSON 字符串），如 '{\"status\":\"pending\",\"batch_id\":1}'",
        default=None,
    )
    p_bt_save.add_argument(
        "-e", "--env",
        help="环境变量白名单（逗号分隔），如 'PYTHONPATH,HOME,AUDIT_DIR'",
        default=None,
    )
    p_bt_save.add_argument(
        "--export-opts",
        help="导出选项（JSON 字符串），如 '{\"include_sources\":true,\"encoding\":\"utf-8\"}'",
        default=None,
    )
    p_bt_save.add_argument(
        "--conflict-default",
        choices=["abort", "save-as", "overwrite"],
        default="abort",
        help="默认冲突策略：abort(中止,默认) / save-as(另存) / overwrite(覆盖)",
    )
    p_bt_save.add_argument(
        "--disabled", action="store_true",
        help="创建时即标记为禁用（默认启用）",
    )
    p_bt_save.set_defaults(func=cmd_bt_save)

    p_bt_list = subparsers.add_parser(
        "batch-template-list",
        help="列出批量任务模板",
    )
    p_bt_list.add_argument(
        "-a", "--all", action="store_true",
        help="显示包括禁用的所有模板（默认只显示启用的）",
    )
    p_bt_list.set_defaults(func=cmd_bt_list)

    p_bt_show = subparsers.add_parser(
        "batch-template-show",
        help="查看批量任务模板详情",
    )
    p_bt_show.add_argument("name", help="模板名称")
    p_bt_show.set_defaults(func=cmd_bt_show)

    p_bt_delete = subparsers.add_parser(
        "batch-template-delete",
        help="删除批量任务模板（可通过 batch-template-undo 撤销）",
    )
    p_bt_delete.add_argument("name", help="模板名称")
    p_bt_delete.set_defaults(func=cmd_bt_delete)

    p_bt_disable = subparsers.add_parser(
        "batch-template-disable",
        help="禁用批量任务模板（可 undo）",
    )
    p_bt_disable.add_argument("name", help="模板名称")
    p_bt_disable.set_defaults(func=cmd_bt_disable)

    p_bt_enable = subparsers.add_parser(
        "batch-template-enable",
        help="启用批量任务模板（可 undo）",
    )
    p_bt_enable.add_argument("name", help="模板名称")
    p_bt_enable.set_defaults(func=cmd_bt_enable)

    p_bt_copy = subparsers.add_parser(
        "batch-template-copy",
        help="复制批量任务模板",
    )
    p_bt_copy.add_argument("source", help="源模板名称")
    p_bt_copy.add_argument("destination", help="目标模板名称（不能已存在）")
    p_bt_copy.set_defaults(func=cmd_bt_copy)

    p_bt_undo = subparsers.add_parser(
        "batch-template-undo",
        help="撤销指定模板的最近一次变更（create/modify/delete/disable/enable）",
    )
    p_bt_undo.add_argument("name", help="模板名称")
    p_bt_undo.set_defaults(func=cmd_bt_undo)

    p_bt_export = subparsers.add_parser(
        "batch-template-export",
        help="把批量任务模板导出为 JSON 配置文件（可分享/备份）",
    )
    p_bt_export.add_argument("name", help="模板名称")
    p_bt_export.add_argument("file", help="输出文件路径(JSON)")
    p_bt_export.set_defaults(func=cmd_bt_export)

    p_bt_import = subparsers.add_parser(
        "batch-template-import",
        help="从 JSON 文件导入批量任务模板，支持 --conflict abort|save-as|overwrite",
    )
    p_bt_import.add_argument("file", help="模板配置文件路径(JSON)")
    p_bt_import.add_argument(
        "--conflict",
        choices=["abort", "save-as", "overwrite"],
        default="abort",
        help=(
            "重名冲突策略："
            "abort(中止,默认) — 同名且内容不同时立即中止，不改动任何数据；"
            "save-as(另存) — 自动另存为 <name>_2, <name>_3 ...；"
            "overwrite(覆盖) — 直接覆盖已有模板（记录 modify 历史，可 undo）"
        ),
    )
    p_bt_import.set_defaults(func=cmd_bt_import)

    return parser


def main(argv: List[str] = None) -> int:
    """主入口函数.

    Args:
        argv: 命令行参数列表

    Returns:
        退出码
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except Exception as e:
        print(f"[ERROR] 未预期的错误: {e}")
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
