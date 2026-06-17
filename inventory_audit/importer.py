"""CSV 导入模块 - 解析盘点 CSV，校验数据并入库."""
import csv
import hashlib
import os
from typing import Any, Dict, List, Tuple

from . import db


def compute_file_hash(file_path: str) -> str:
    """计算文件的 SHA256 哈希值.

    Args:
        file_path: 文件路径

    Returns:
        十六进制哈希字符串
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def validate_row(
    row: Dict[str, str],
    csv_config: Dict[str, Any],
    line_number: int,
) -> Tuple[bool, str, Dict[str, Any]]:
    """校验一行数据的合法性.

    缺少 counted_qty 时明确报错并阻止入库，不默默按 0 处理。

    Args:
        row: CSV 行数据
        csv_config: CSV 列配置
        line_number: 行号

    Returns:
        (是否合法, 错误信息, 解析后的数据字典)
    """
    loc_col = csv_config["location_column"]
    sku_col = csv_config["sku_column"]
    exp_col = csv_config["expected_column"]
    cnt_col = csv_config["counted_column"]

    location = (row.get(loc_col) or "").strip()
    sku = (row.get(sku_col) or "").strip()

    if not sku:
        return False, f"第 {line_number} 行: SKU 为空", {}

    if not location:
        return False, f"第 {line_number} 行: 库位为空", {}

    try:
        expected_qty = float(row.get(exp_col) or 0)
    except (ValueError, TypeError):
        return False, f"第 {line_number} 行: 账面数量非法 - {row.get(exp_col)}", {}

    raw_counted = row.get(cnt_col)
    if raw_counted is None or str(raw_counted).strip() == "":
        return False, f"第 {line_number} 行: 实盘数量(counted_qty)为空，无法导入", {}

    try:
        counted_qty = float(raw_counted)
    except (ValueError, TypeError):
        return False, f"第 {line_number} 行: 实盘数量非法 - {raw_counted}", {}

    diff_qty = counted_qty - expected_qty

    parsed = {
        "location": location,
        "sku": sku,
        "expected_qty": expected_qty,
        "counted_qty": counted_qty,
        "diff_qty": diff_qty,
        "line_number": line_number,
        "raw_data": str(row),
    }
    return True, "", parsed


def compute_merge_key(parsed_row: Dict[str, Any], merge_keys: List[str]) -> str:
    """根据配置的合并键计算 merge_key 字符串.

    合并键由 rules.merge_keys 驱动，而非硬编码 location+sku。

    Args:
        parsed_row: 解析后的行数据
        merge_keys: 合并键字段名列表

    Returns:
        合并键字符串
    """
    parts = [str(parsed_row.get(key, "")) for key in merge_keys]
    return "|".join(parts)


def import_csv(
    db_path: str,
    csv_path: str,
    csv_config: Dict[str, Any],
    batch_name: str = None,
    default_status: str = "pending",
    rules: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """导入盘点 CSV 文件.

    规则配置驱动阈值过滤和合并键计算：
    - rules.diff_threshold: 差异绝对值低于阈值的不入库
    - rules.merge_keys: 决定合并分组的字段

    Args:
        db_path: 数据库路径
        csv_path: CSV 文件路径
        csv_config: CSV 列配置
        batch_name: 批次名称，默认使用文件名
        default_status: 新差异的默认状态
        rules: 规则配置

    Returns:
        导入结果字典
    """
    if rules is None:
        rules = {"diff_threshold": 0, "merge_keys": ["location", "sku"]}
    diff_threshold = float(rules.get("diff_threshold", 0))
    merge_keys = list(rules.get("merge_keys", ["location", "sku"]))

    csv_path = os.path.abspath(csv_path)

    if not os.path.exists(csv_path):
        return {
            "success": False,
            "error": f"文件不存在: {csv_path}",
            "batch_id": None,
            "imported": 0,
            "skipped": 0,
            "errors": [],
        }

    file_hash = compute_file_hash(csv_path)
    existing = db.check_batch_exists(db_path, file_hash)
    if existing:
        return {
            "success": False,
            "error": f"文件已导入过，批次 ID: {existing['id']}, 名称: {existing['batch_name']}",
            "batch_id": existing["id"],
            "imported": 0,
            "skipped": 0,
            "errors": [],
            "duplicate": True,
        }

    if not batch_name:
        batch_name = os.path.splitext(os.path.basename(csv_path))[0]

    encoding = csv_config.get("encoding", "utf-8-sig")
    delimiter = csv_config.get("delimiter", ",")
    cnt_col = csv_config["counted_column"]

    valid_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    zero_diff_count = 0
    below_threshold_count = 0

    with open(csv_path, "r", encoding=encoding) as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        fieldnames = reader.fieldnames or []
        if cnt_col not in fieldnames:
            return {
                "success": False,
                "error": f"CSV 缺少必需列: '{cnt_col}'，无法导入",
                "batch_id": None,
                "imported": 0,
                "skipped": 0,
                "errors": [f"缺少列: {cnt_col}"],
            }

        for i, row in enumerate(reader, start=2):
            is_valid, err_msg, parsed = validate_row(row, csv_config, i)
            if not is_valid:
                errors.append(err_msg)
                continue

            if parsed["diff_qty"] == 0:
                zero_diff_count += 1
                continue

            if diff_threshold > 0 and abs(parsed["diff_qty"]) < diff_threshold:
                below_threshold_count += 1
                continue

            parsed["merge_key"] = compute_merge_key(parsed, merge_keys)
            valid_rows.append(parsed)

    if not valid_rows:
        return {
            "success": False,
            "error": "没有有效的差异数据行",
            "batch_id": None,
            "imported": 0,
            "skipped": zero_diff_count,
            "below_threshold_skipped": below_threshold_count,
            "errors": errors,
        }

    batch_id = db.create_batch(db_path, batch_name, csv_path, file_hash)
    source_ids = db.insert_source_lines(db_path, batch_id, valid_rows)

    for i, row in enumerate(valid_rows):
        db.upsert_difference(
            db_path,
            row["location"],
            row["sku"],
            row["merge_key"],
            row["diff_qty"],
            source_ids[i],
            default_status,
        )

    return {
        "success": True,
        "batch_id": batch_id,
        "batch_name": batch_name,
        "imported": len(valid_rows),
        "zero_diff_skipped": zero_diff_count,
        "below_threshold_skipped": below_threshold_count,
        "error_count": len(errors),
        "errors": errors,
    }
