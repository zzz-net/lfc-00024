# 仓库盘点差异复核 CLI 使用说明

## 概述

这是一个仓库盘点差异复核工具，支持多批次 CSV 导入、差异自动合并、状态复核、备注记录、操作撤销和报告导出。所有数据持久化在 SQLite 数据库中，中断后可继续，支持逐步撤销。

## 快速开始

```bash
# 1. 初始化（首次使用）
python -m inventory_audit -c samples/config.json init

# 2. 导入第一批盘点 CSV
python -m inventory_audit -c samples/config.json import samples/batch1.csv -n 2024-01-上午盘

# 3. 导入第二批盘点 CSV
python -m inventory_audit -c samples/config.json import samples/batch2.csv -n 2024-01-下午盘

# 4. 查看差异列表
python -m inventory_audit -c samples/config.json list

# 5. 查看单个差异详情
python -m inventory_audit -c samples/config.json show 1

# 6. 标记为已确认
python -m inventory_audit -c samples/config.json status 1 confirmed

# 7. 添加备注
python -m inventory_audit -c samples/config.json remark 1 "已与仓库核对，盘亏5件"

# 8. 撤销上一步
python -m inventory_audit -c samples/config.json undo

# 9. 导出报告
python -m inventory_audit -c samples/config.json export -t differences
python -m inventory_audit -c samples/config.json export -t summary
```

## 命令详解

### 1. `init` - 初始化

创建数据库文件和必要目录。

```bash
python -m inventory_audit init
```

### 2. `import` - 导入盘点 CSV

导入一份盘点 CSV 文件，自动合并到现有差异中。

```bash
python -m inventory_audit import <csv文件路径> [-n 批次名称]
```

**特性：**
- 自动检测重复导入（通过文件哈希），同一文件不会重复导入
- 自动校验：SKU 为空、数量非法的行会被跳过并报错
- 零差异行自动跳过，不进入差异库
- 同一库位 + SKU 的差异自动合并，保留所有来源行

### 3. `list` - 列出差异

```bash
python -m inventory_audit list [-s 状态] [-l 库位] [--sku SKU]
```

**状态过滤值：** `pending` (待处理), `confirmed` (已确认), `ignored` (忽略), `closed` (已关闭)

### 4. `show` - 查看差异详情

```bash
python -m inventory_audit show <差异ID>
```

显示：基本信息、所有来源行明细、复核历史记录。

### 5. `status` - 设置差异状态

```bash
# 单条
python -m inventory_audit status <差异ID> <状态>

# 批量
python -m inventory_audit status <ID1> <ID2> <ID3> <状态>
```

**可用状态：**
- `pending` - 待处理
- `confirmed` - 已确认
- `ignored` - 忽略
- `closed` - 已关闭

每次状态变更都会记录到历史，可通过 `undo` 撤销。

### 6. `remark` - 设置备注

```bash
python -m inventory_audit remark <差异ID> "<备注内容>"
```

备注变更也会记录历史，可撤销。

### 7. `undo` - 撤销上一步

```bash
python -m inventory_audit undo
```

撤销最近一次状态变更或备注修改。空历史时安全返回提示，不会报错。

### 8. `history` - 查看复核历史

```bash
python -m inventory_audit history [-d 差异ID] [-n 条数]
```

### 9. `export` - 导出报告

```bash
# 导出差异明细（默认）
python -m inventory_audit export [-t differences] [-s 状态]

# 导出汇总统计
python -m inventory_audit export -t summary

# 导出来源行明细
python -m inventory_audit export -t sources [-b 批次ID]
```

导出文件保存在配置的 `export.output_dir` 目录，文件名带时间戳。

### 10. `batches` - 查看批次列表

```bash
python -m inventory_audit batches
```

### 11. `summary` - 查看汇总统计

```bash
python -m inventory_audit summary
```

### 12. `remerge` - 重新合并差异

```bash
python -m inventory_audit remerge [-f]
```

数据修复用。重新根据来源行计算差异总量，保留已有状态和备注。

## 配置说明

配置文件为 JSON 格式，示例见 `samples/config.json`。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `database.path` | SQLite 数据库文件路径 | `./audit_data/audit.db` |
| `csv.location_column` | 库位列名 | `location` |
| `csv.sku_column` | SKU 列名 | `sku` |
| `csv.expected_column` | 账面数量列名 | `expected_qty` |
| `csv.counted_column` | 实盘数量列名 | `counted_qty` |
| `csv.encoding` | CSV 文件编码 | `utf-8-sig` |
| `csv.delimiter` | CSV 分隔符 | `,` |
| `status.initial` | 新差异默认状态 | `pending` |
| `export.output_dir` | 报告输出目录 | `./audit_data/exports` |

## 数据模型

### 核心概念

- **批次 (Batch)**: 每次导入的 CSV 对应一个批次，有唯一文件哈希去重
- **来源行 (Source Line)**: CSV 中的每一行原始数据（有差异的才保留）
- **差异 (Difference)**: 按「库位 + SKU」合并后的差异记录，关联所有来源行
- **复核历史 (Review History)**: 每次状态/备注变更的记录，用于撤销

### 状态流转

```
pending (待处理)
    ├──> confirmed (已确认)
    ├──> ignored (忽略)
    └──> closed (已关闭)
```

所有状态之间可互相转换，每次变更都可撤销。

## 异常场景处理

| 场景 | 行为 |
|------|------|
| SKU 为空 | 跳过该行，记录错误信息 |
| 数量非法（非数字） | 跳过该行，记录错误信息 |
| 零差异行 | 自动跳过，不进入差异库 |
| 重复导入同一文件 | 提示已导入，返回原批次 ID，不破坏数据 |
| 撤销空历史 | 返回友好提示，不报错 |
| 程序中断后重启 | 数据已持久化，继续操作即可 |
| 多批次合并 | 同一库位+SKU 自动累加差异量，保留所有来源 |

## 目录结构

```
inventory_audit/         # 主包
  __init__.py
  __main__.py
  cli.py                 # CLI 入口
  config.py              # 配置加载
  db.py                  # 数据库操作
  importer.py            # CSV 导入
  merger.py              # 差异合并与查询
  reviewer.py            # 复核操作（状态、备注、撤销）
  exporter.py            # 报告导出

samples/                 # 样例文件
  config.json            # 样例配置
  batch1.csv             # 第一批盘点样例
  batch2.csv             # 第二批盘点样例

audit_data/              # 运行时数据（自动创建）
  audit.db               # SQLite 数据库
  exports/               # 导出报告
```
