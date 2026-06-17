"""CLI 入口 - 仓库盘点差异复核工具."""
import argparse
import os
import sys
from typing import Any, Dict, List

from . import config as cfg
from . import db
from . import importer
from . import merger
from . import reviewer
from . import exporter


def _load_config(args) -> Dict[str, Any]:
    """加载配置并确保目录存在."""
    config = cfg.load_config(getattr(args, "config", None))
    cfg.ensure_dirs(config)
    return config


def _get_db_path(config: Dict[str, Any]) -> str:
    return cfg.get_db_path(config)


def cmd_init(args) -> int:
    """初始化命令 - 创建数据库和目录."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)
    print(f"[OK] 初始化完成")
    print(f"  数据库: {db_path}")
    print(f"  导出目录: {os.path.abspath(config['export']['output_dir'])}")
    return 0


def cmd_import(args) -> int:
    """导入 CSV 命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)
    db.init_db(db_path)

    csv_path = args.csv_file
    batch_name = getattr(args, "name", None)

    result = importer.import_csv(
        db_path, csv_path, config["csv"],
        batch_name=batch_name,
        default_status=config["status"]["initial"],
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

    print(f"[OK] 导入成功")
    print(f"  批次 ID: {result['batch_id']}")
    print(f"  批次名称: {result['batch_name']}")
    print(f"  导入差异行: {result['imported']}")
    print(f"  零差异跳过: {result['zero_diff_skipped']}")

    if result.get("error_count", 0) > 0:
        print(f"  数据错误: {result['error_count']} 条")
        for err in result["errors"][:5]:
            print(f"    - {err}")
        if result["error_count"] > 5:
            print(f"    ... 还有 {result['error_count'] - 5} 条")

    return 0


def cmd_list(args) -> int:
    """列出差异命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    status = getattr(args, "status", None)
    location = getattr(args, "location", None)
    sku = getattr(args, "sku", None)

    diffs = merger.get_merged_differences(
        db_path, status=status, location=location, sku=sku
    )

    if not diffs:
        print("没有找到差异记录")
        return 0

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
            print(f"  [{h['created_at']}] {desc} ({h['operator']})")

    return 0


def cmd_status(args) -> int:
    """设置状态命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    diff_ids = args.diff_ids
    status = args.status

    if len(diff_ids) == 1:
        result = reviewer.set_status(db_path, diff_ids[0], status)
    else:
        result = reviewer.batch_set_status(db_path, diff_ids, status)

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

    return 0


def cmd_remark(args) -> int:
    """设置备注命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    diff_id = args.diff_id
    remark = args.remark

    result = reviewer.set_remark(db_path, diff_id, remark)

    if not result.get("success"):
        print(f"[ERROR] {result.get('error', '操作失败')}")
        return 1

    if result.get("skipped"):
        print(f"[SKIP] {result.get('message', '')}")
        return 0

    print(f"[OK] 备注已更新")
    print(f"  差异 #{result['diff_id']}")
    return 0


def cmd_undo(args) -> int:
    """撤销命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    result = reviewer.undo_last(db_path)

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
        print(f"[{h['created_at']}] #{h['difference_id']} "
              f"({h['location']}/{h['sku']}): {desc}")

    return 0


def cmd_export(args) -> int:
    """导出命令."""
    config = _load_config(args)
    db_path = _get_db_path(config)

    output_dir = os.path.abspath(config["export"]["output_dir"])
    status = getattr(args, "status", None)

    export_type = getattr(args, "type", "differences")

    if export_type == "differences":
        result = exporter.export_differences(
            db_path, output_dir, status=status, include_sources=True
        )
    elif export_type == "summary":
        result = exporter.export_summary(db_path, output_dir)
    elif export_type == "sources":
        batch_id = getattr(args, "batch_id", None)
        result = exporter.export_source_lines(db_path, output_dir, batch_id=batch_id)
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

    result = merger.remerge_all(db_path)

    print(f"[OK] 重新合并完成")
    print(f"  差异总数: {result['total_differences']}")
    print(f"  保留状态: {result['preserved_status']}")
    print(f"  来源行数: {result['source_lines']}")
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

    # init
    p_init = subparsers.add_parser("init", help="初始化数据库和目录")
    p_init.set_defaults(func=cmd_init)

    # import
    p_import = subparsers.add_parser("import", help="导入盘点 CSV")
    p_import.add_argument("csv_file", help="CSV 文件路径")
    p_import.add_argument("-n", "--name", help="批次名称", default=None)
    p_import.set_defaults(func=cmd_import)

    # list
    p_list = subparsers.add_parser("list", help="列出差异")
    p_list.add_argument("-s", "--status", help="按状态过滤", default=None)
    p_list.add_argument("-l", "--location", help="按库位过滤", default=None)
    p_list.add_argument("--sku", help="按 SKU 过滤", default=None)
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = subparsers.add_parser("show", help="查看差异详情")
    p_show.add_argument("diff_id", type=int, help="差异 ID")
    p_show.set_defaults(func=cmd_show)

    # status
    p_status = subparsers.add_parser("status", help="设置差异状态")
    p_status.add_argument("diff_ids", type=int, nargs="+", help="差异 ID（可多个）")
    p_status.add_argument(
        "status",
        choices=["pending", "confirmed", "ignored", "closed"],
        help="状态: pending(待处理), confirmed(已确认), ignored(忽略), closed(已关闭)",
    )
    p_status.set_defaults(func=cmd_status)

    # remark
    p_remark = subparsers.add_parser("remark", help="设置差异备注")
    p_remark.add_argument("diff_id", type=int, help="差异 ID")
    p_remark.add_argument("remark", help="备注内容")
    p_remark.set_defaults(func=cmd_remark)

    # undo
    p_undo = subparsers.add_parser("undo", help="撤销最后一次复核操作")
    p_undo.set_defaults(func=cmd_undo)

    # history
    p_history = subparsers.add_parser("history", help="查看复核历史")
    p_history.add_argument("-d", "--diff-id", type=int, help="指定差异 ID", default=None)
    p_history.add_argument("-n", "--limit", type=int, help="显示条数", default=20)
    p_history.set_defaults(func=cmd_history)

    # export
    p_export = subparsers.add_parser("export", help="导出报告")
    p_export.add_argument(
        "-t", "--type",
        choices=["differences", "summary", "sources"],
        default="differences",
        help="导出类型: differences(差异明细), summary(汇总), sources(来源行)",
    )
    p_export.add_argument("-s", "--status", help="按状态过滤", default=None)
    p_export.add_argument("-b", "--batch-id", type=int, help="批次 ID (sources 类型)", default=None)
    p_export.set_defaults(func=cmd_export)

    # batches
    p_batches = subparsers.add_parser("batches", help="查看批次列表")
    p_batches.set_defaults(func=cmd_batches)

    # summary
    p_summary = subparsers.add_parser("summary", help="查看汇总统计")
    p_summary.set_defaults(func=cmd_summary)

    # remerge
    p_remerge = subparsers.add_parser("remerge", help="重新合并差异（数据修复用）")
    p_remerge.add_argument("-f", "--force", action="store_true", help="跳过确认")
    p_remerge.set_defaults(func=cmd_remerge)

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
