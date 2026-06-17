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

### 13. `set-operator` - 设置操作人

```bash
python -m inventory_audit set-operator <操作人名称>
```

操作人会被记录到每条状态变更、备注、撤销、导出操作中，并持久化到 `runtime_state.json`，重启后仍生效。

### 14. `plan-save` - 保存复核方案

```bash
python -m inventory_audit plan-save <方案名> \
    [-s 状态过滤] [-l 库位过滤] [--sku SKU过滤] \
    [-f 导出字段(逗号分隔)] [-r 备注模板]
```

将筛选条件、导出字段、备注模板保存为方案。方案双写到数据库和 `audit_data/plans/<方案名>.json`，即使数据库被重建也能从 JSON 回补。

常用导出字段（默认全部）：`id,location,sku,total_diff_qty,status,remark,batch_names,source_count,created_at,updated_at,merge_key`。

### 15. `plan-list` - 列出所有方案

```bash
python -m inventory_audit plan-list
```

显示所有方案名，当前激活方案用 `*` 标记。

### 16. `plan-use` - 激活方案

```bash
python -m inventory_audit plan-use [方案名]
```

激活指定方案后，`list`、`export` 会自动套用该方案的筛选条件和导出字段；不带参数则清除激活方案。激活状态落盘到 `runtime_state.json`，重启后续用。

**重要：切换方案只影响视图筛选与导出字段，绝不修改已有批次编号或汇总统计。**

### 17. `plan-delete` - 删除方案

```bash
python -m inventory_audit plan-delete <方案名>
```

同时删除数据库记录和 JSON 落盘文件。若该方案正处于激活状态，会一并清除激活。

### 18. `replay` - 按操作日志回放

```bash
python -m inventory_audit replay [-p 方案名] [-o 操作人] [-r keep|snapshot|abort]
```

按时间升序回放符合条件的操作日志（状态变更、备注、撤销、导出）。

**冲突处理策略**（同一差异在日志记录后又被其他方案/操作者改动时触发）：
- `abort`（默认）：遇到冲突立即中止，保留已成功回放的操作
- `keep`：跳过冲突条目，保留当前数据库状态
- `snapshot`：将当前差异状态另存为 `exports/snapshot_diff<ID>_<时间戳>_conflict.json`，然后跳过该条

### 19. `template-save` - 保存复核方案模板（版本化）

```bash
python -m inventory_audit template-save <模板名> \
    [-s 状态过滤] [-l 库位过滤] [--sku SKU过滤] \
    [-f 导出字段(逗号分隔)] [-r 备注模板] \
    [-a list,export,replay(逗号分隔)]
```

把筛选条件、导出字段、备注模板、批量执行动作打包为可复用模板，带自增版本号。
`-a` 指定该模板的批量执行顺序（默认 `list,export,replay`）。

### 20. `template-list` / `template-show` / `template-delete`

```bash
python -m inventory_audit template-list
python -m inventory_audit template-show <模板名>
python -m inventory_audit template-delete <模板名>
```

`template-show` 会显示最新版本的完整内容，包括最近一次执行记录（如有）。

### 21. `template-import` / `template-export`

```bash
python -m inventory_audit template-import <json文件> [--force]
python -m inventory_audit template-export <模板名> <输出文件路径>
```

用于跨环境迁移模板。导入时自动处理重名和版本冲突，同名但内容不同时需加 `--force` 覆盖（会 bump 版本号）。

### 22. `template-run` - 按模板批量执行

```bash
python -m inventory_audit template-run <模板名> [--execution-id ID] [--resume]
```

按模板 action 顺序批量执行 list/export/replay，每次运行产生一条执行记录。

**执行状态（统一术语，共 4 种）**：

| 状态值 | 中文说明 | 返回码 | 是否可续跑 |
|--------|----------|--------|-----------|
| `pending` | 待执行（已创建未开始） | - | ✅ 是 |
| `running` | 执行中（进程被外部终止） | - | ✅ 是 |
| `failed` | 执行中断（某步失败，已完成步骤保留） | 2 | ✅ 是 |
| `completed` | 全部步骤成功完成 | 0 | ❌ 否 |

**【续跑条件】统一规则**：只要执行状态 **不是 completed**，就可以用 `--resume` 续跑。
续跑时，状态为 `done` 的已完成步骤会被标记为 `skipped_done` 跳过，**不重复产生日志或导出文件**；
状态为 `failed`/`running`/`pending` 的步骤从当前位置继续执行。

- `--resume`：续跑最近一次未完成的执行（从失败步骤继续）
- `--execution-id`：续跑指定 ID 的执行记录

**返回码说明**：
- `0`：所有步骤完成（状态 `completed`）
- `1`：命令参数错误或环境问题
- `2`：某步执行失败（状态 `failed`），可使用 `--resume` 续跑

### 23. `template-export-execution` - 导出执行归档清单

```bash
python -m inventory_audit template-export-execution <模板名> \
    [-e 执行ID] [-o 输出路径]
```

把一次模板执行的完整上下文打包为 JSON：模板快照、步骤结果、operator、
激活方案、所有导出记录、操作日志。默认输出到 `archives/` 目录。

- 不传 `-e` 时导出该模板最近一次执行
- 传 `-o` 时写到指定路径

### 24. `template-preview-archive` - 预检/预览归档清单

```bash
python -m inventory_audit template-preview-archive <归档文件> \
    [--no-conflict-check]
```

---
**⚠️ 恢复前必做步骤**

在运行 `template-restore-execution` 之前，必须先运行此命令预检。
不执行任何恢复操作，不改动任何数据，只让你看清归档里带了哪些执行信息、
恢复后可能碰到什么冲突，再决定使用 `--conflict abort` 还是 `save-as`。

---

**输出内容结构（按顺序显示）**：

| 输出区块 | 内容说明 |
|----------|----------|
| **归档基本信息** | 文件路径、清单 schema 版本、归档导出时间 |
| **执行信息** | 原执行 ID、状态(failed/completed/running/pending)、开始/结束时间、步骤统计(done/total/failed)、操作人、激活方案 |
| **模板快照** | 模板名称、版本、描述、内容指纹、筛选条件(status/location/sku)、导出字段、备注模板、步骤数量 |
| **执行步骤详情** | 每步的序号、状态标签([完成]/[失败/中断]/[跳过/已完成]/[待执行]/[运行中])、动作名（含子类型）、导出文件名、错误信息（如有） |
| **【执行状态说明】** | **新增统一术语提示**：当前状态中文名 + 是否可续跑；续跑条件：状态 ≠ completed；续跑时已完成步骤标记为 skipped_done，不重复导出 |
| **导出文件列表** | 每步导出的文件名、文件类型(summary/differences/sources)、文件大小、磁盘上是否仍存在 |
| **操作日志数量** | 与该模板相关的导出操作日志条数 |
| **恢复冲突检测** | 仅当有数据库时显示；区分「阻塞冲突」和「提示冲突」；每项含类型、说明、save-as 自动处理方式 |
| **建议下一步** | 根据冲突情况和执行状态给出建议命令 |

**步骤状态标签（共 5 种，与内部状态一一对应）**：
| 标签显示 | 对应内部状态 | 含义 | 续跑时行为 |
|----------|-------------|------|-----------|
| `[完成]` | `done` | 步骤执行成功 | 标记 `skipped_done`，不重跑 |
| `[失败/中断]` | `failed` | 步骤执行失败或异常中断 | 从这一步开始重跑 |
| `[跳过/已完成]` | `skipped_done` | 续跑时因已完成而跳过 | 保持跳过 |
| `[待执行]` | `pending` | 步骤尚未开始执行 | 从这一步开始执行 |
| `[运行中]` | `running` | 步骤开始但未持久化完成（进程被 kill） | 从这一步重新执行 |

**阻塞冲突类型（共 3 类）**：
1. `template_upgraded`：当前环境同名模板已升级（版本/内容不同）
2. `export_file_exists`：归档中导出的 CSV 在磁盘上已存在
3. `active_plan_mismatch`：归档记录的激活方案与当前 runtime 不同

**参数说明**：
- `--no-conflict-check`：离线场景使用。跳过数据库连接，仅显示归档内容摘要，不检测冲突（也不输出「恢复冲突检测」和具体建议命令）

### 25. `template-restore-execution` - 从归档清单恢复执行历史

```bash
python -m inventory_audit template-restore-execution <归档文件> \
    [--conflict abort|save-as]
```

把归档清单还原到当前环境：重建模板（如缺失）、恢复执行记录、步骤结果、
导出记录、操作日志。

---
**⚠️ 恢复前必须先预检**

必须先用 `template-preview-archive` 预览归档内容和冲突，确认无误后再执行恢复。

---

**阻塞冲突类型 × 风险等级**：

| 冲突类型 | 风险说明 | abort 行为 | save-as 自动处理方式 |
|----------|----------|-----------|---------------------|
| `template_upgraded` | 当前环境同名模板已升级（版本/内容不同），恢复旧版本会覆盖已升级的模板定义 | 立即中止 | 模板另存为 `<name>_restored`（冲突则 `_restored[2|3…]`），不改动现有模板 |
| `export_file_exists` | 归档中导出的 CSV 在磁盘上已存在，恢复会造成文件名重复或意外覆盖 | 立即中止 | **仅恢复元数据**到数据库，磁盘现有文件保持不变（不覆盖） |
| `active_plan_mismatch` | 归档记录的激活方案与当前 runtime 不同，直接恢复会混淆方案归属 | 立即中止 | 恢复的执行记录保留归档的 active_plan，**不修改当前 runtime** 激活方案 |

**冲突策略行为详表**：

| 策略 | 有阻塞冲突时 | 无冲突时 | 数据改动保证 |
|------|-------------|---------|-------------|
| `--conflict abort`（默认） | 立即中止，返回码 2 | 正常恢复，返回码 0 | **不改动任何数据**，abort 发生时 DB/templates/磁盘文件全部不变 |
| `--conflict save-as` | 按上表逐一自动处理，处理成功后继续恢复 | 正常恢复，返回码 0 | 模板可能新增（`_restored` 后缀名），DB 写入恢复数据，磁盘文件从不覆盖 |

**恢复后状态与续跑条件**：
- 恢复的执行记录状态与归档完全一致（`pending`/`running`/`failed`/`completed`）
- **【续跑条件-统一规则】**：只要状态 **不是 completed**，就可以 `--resume` 续跑
- 续跑时，已完成步骤（status=done）被标记为 `skipped_done`，不重复产生日志或导出文件

**建议操作顺序（6 步）**：
1. 用 `template-preview-archive` 预览归档内容和冲突
2. 根据冲突情况选择 `--conflict` 策略：无冲突/想自动处理 → `save-as`；不想自动改动 → `abort`
3. 执行恢复
4. 用 `template-show` 验证恢复结果（模板名、版本、operator、步骤统计、执行状态）
5. 若状态不是 completed，用 `template-run --resume` 续跑
6. 用 `template-export-execution` 再次导出归档，核对元数据与原归档一致

**恢复后验证命令**：
```bash
# 查看恢复后的执行摘要（模板名、版本、operator、步骤状态、续跑命令提示）
python -m inventory_audit template-show <模板名>

# 【续跑条件】恢复的执行若状态不是 completed，可续跑（failed/running/pending 均可）
python -m inventory_audit template-run <模板名> --resume

# 恢复完成的执行可再次导出，元数据应与原归档一致
python -m inventory_audit template-export-execution <模板名>
```


## 完整操作链路指南

以下是几个典型场景的完整操作步骤，按顺序执行即可完整走通。

### 场景一：新建模板 → 批量执行 → 中断续跑 → 导出归档

```bash
# 1. 初始化（首次使用）
python -m inventory_audit -c samples/config.json init

# 2. 导入盘点数据
python -m inventory_audit -c samples/config.json import samples/batch1.csv -n 2024-01-上午盘

# 3. 保存模板（版本化）
python -m inventory_audit -c samples/config.json template-save daily_report \
    -s pending \
    -f id,location,sku,total_diff_qty,status,remark \
    --steps "list,export:summary,export:differences" \
    -d "每日待处理差异报告"

# 4. 查看模板
python -m inventory_audit -c samples/config.json template-show daily_report

# 5. 按模板批量执行
python -m inventory_audit -c samples/config.json template-run daily_report

# 6. 【统一术语说明】
#    - 成功完成：返回码 0，状态 = completed（无需续跑）
#    - 某步失败：返回码 2，状态 = failed（执行中断，可续跑）
#    - 续跑条件：状态「不是 completed」（pending/running/failed 三种都能 --resume）
#    - 续跑保证：已完成步骤标记为 skipped_done，不重复写日志或导出文件
#    如果执行中断（返回码 2，状态 = failed），续跑
python -m inventory_audit -c samples/config.json template-run daily_report --resume

# 7. 导出执行归档
python -m inventory_audit -c samples/config.json template-export-execution daily_report \
    -o archives/daily_report_20240101.json
```

### 场景二：跨环境迁移模板 → 导入后再执行

```bash
# 【环境 A】导出模板
python -m inventory_audit -c config.json template-export daily_report templates/daily_report.json

# 【环境 B】初始化
python -m inventory_audit -c config.json init

# 【环境 B】导入盘点数据
python -m inventory_audit -c config.json import batch1.csv -n 2024-01-上午盘

# 【环境 B】导入模板
python -m inventory_audit -c config.json template-import templates/daily_report.json

# 【环境 B】查看导入的模板
python -m inventory_audit -c config.json template-show daily_report

# 【环境 B】按模板批量执行
python -m inventory_audit -c config.json template-run daily_report

# 【环境 B】导出执行归档
python -m inventory_audit -c config.json template-export-execution daily_report
```

### 场景三：机器重启/数据丢失 → 从归档恢复 → 续跑 → 再次导出（最关键链路）

```bash
# 【故障前】已有归档文件：archives/daily_report_20240101.json

# 1. 初始化新环境或重建数据库
python -m inventory_audit -c config.json init

# 2. 重新导入盘点数据（必须与原执行时的数据一致）
python -m inventory_audit -c config.json import batch1.csv -n 2024-01-上午盘

# 3. 【关键】预览归档，看清内容和可能的冲突
python -m inventory_audit -c config.json template-preview-archive archives/daily_report_20240101.json

# 4. 根据预览结果选择冲突策略，执行恢复
#    无冲突：直接恢复
python -m inventory_audit -c config.json template-restore-execution archives/daily_report_20240101.json

#    有冲突且想自动处理：使用 save-as
python -m inventory_audit -c config.json template-restore-execution archives/daily_report_20240101.json \
    --conflict save-as

# 5. 验证恢复结果
python -m inventory_audit -c config.json template-show daily_report

# 6. 如果原执行是 interrupted 状态，续跑未完成的步骤
python -m inventory_audit -c config.json template-run daily_report --resume

# 7. 再次导出归档，验证元数据一致
python -m inventory_audit -c config.json template-export-execution daily_report \
    -o archives/daily_report_restored.json
```

### 场景四：离线预览归档（无数据库环境）

```bash
# 只看归档内容，不检测冲突（不需要数据库）
python -m inventory_audit template-preview-archive archives/daily_report_20240101.json \
    --no-conflict-check
```

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
| `active_plan` | 当前激活方案（运行时状态，自动落盘） | `null` |
| `operator` | 当前操作人（运行时状态，自动落盘） | `cli` |

**运行时状态文件：** `audit_data/runtime_state.json`，保存 `active_plan` 与 `operator`。

**方案落盘目录：** `audit_data/plans/<方案名>.json`，方案 JSON 双写冗余。

## 数据模型

### 核心概念

- **批次 (Batch)**: 每次导入的 CSV 对应一个批次，有唯一文件哈希去重
- **来源行 (Source Line)**: CSV 中的每一行原始数据（有差异的才保留）
- **差异 (Difference)**: 按配置 `rules.merge_keys` 合并后的差异记录，关联所有来源行
- **复核历史 (Review History)**: 每次状态/备注变更的记录，用于撤销；关联 `plan_id` / `plan_name`
- **复核方案 (Plan)**: 筛选条件、导出字段、备注模板的集合；持久化在数据库 + JSON
- **操作日志 (Operation Log)**: 所有 status_change / remark_change / undo / export 的不可变记录，用于回放
- **操作人 (Operator)**: 每条操作的执行者，落盘到 runtime_state.json
- **复核方案模板 (Template)**: 方案筛选 + 导出字段 + 执行动作的打包，带版本号，可导入导出
- **模板执行记录 (Execution)**: 一次 `template-run` 的完整快照（步骤结果、状态、operator、激活方案）
- **执行归档清单 (Archive)**: 执行记录 + 模板快照 + 操作日志的 JSON 打包，可离线恢复

### 状态流转

```
pending (待处理)
    ├──> confirmed (已确认)
    ├──> ignored (忽略)
    └──> closed (已关闭)
```

所有状态之间可互相转换，每次变更都可撤销。

### 导出一致性

所有导出 CSV 文件：
1. 文件名带 `_plan<ID>` 后缀（如激活方案），区分不同方案的导出结果
2. 首行是元数据注释：`# 导出时间 / 方案 / 操作人 / 状态过滤 / 导出字段`
3. 差异导出按方案 `export_fields` 决定列顺序，缺失字段自动为空
4. 导出操作会写入 `operation_logs`，回放时可重新导出相同文件

## 异常场景处理

| 场景 | 行为 | 状态 / 返回值 |
|------|------|---------------|
| SKU 为空 | 跳过该行，记录错误信息 | 返回码 0 |
| 数量非法（非数字） | 跳过该行，记录错误信息 | 返回码 0 |
| 零差异行 | 自动跳过，不进入差异库 | 返回码 0 |
| 重复导入同一文件 | 提示已导入，返回原批次 ID，不破坏数据 | 返回码 0 |
| 撤销空历史 | 返回友好提示，不报错 | 返回码 0 |
| 程序中断后重启 | 数据已持久化，继续操作即可；方案/操作人自动恢复 | - |
| 多批次合并 | 同一 merge_key 自动累加差异量，保留所有来源 | - |
| 切换方案后再导入 | 旧批次 ID、名称、汇总绝对不变，只新增新批次 | - |
| 回放遇到跨方案/跨操作者冲突 | 按 `-r` 策略处理：abort / keep / snapshot | abort 返回非 0 |
| 数据库重建但 plans/*.json 还在 | 下次 `get_plan` 自动从 JSON 回补到数据库 | - |
| 模板批量执行中途中断 | 执行记录状态保留为 **`failed`**（执行中断），已完成步骤不重复产生日志 | 执行状态: `failed`；返回码 2 |
| 恢复归档时模板已升级/导出文件已存在/激活方案不一致 | 按 `--conflict` 处理：abort（默认，中止不改动任何数据）/ save-as（另存为新模板名，不覆盖磁盘文件） | abort: 返回码 2；save-as: 返回码 0 |
| 归档恢复后执行状态为 **不是 completed** | 直接 `template-run --resume` 从断点继续；已完成步骤标记为 `skipped_done`，不重复导出 | `pending`/`running`/`failed` 均可续跑 |

---

**续跑核心保证**：
1. `--resume` 判定条件：**执行状态不是 completed** 就可以续跑（pending/running/failed 三种均可）
2. 已完成步骤（status=done）续跑时被标记为 `skipped_done`，**不会**重复产生日志或写入导出文件
3. `failed` 步骤被重跑，`running`/`pending` 步骤从当前位置继续执行

---

**执行状态速查表（4 种）**：
| 状态值 | 中文 | 是否可续跑 |
|--------|------|-----------|
| `pending` | 待执行 | ✅ 是 |
| `running` | 执行中 | ✅ 是 |
| `failed` | 执行中断 | ✅ 是 |
| `completed` | 已完成 | ❌ 否 |

## 目录结构

```
inventory_audit/         # 主包
  __init__.py
  __main__.py
  cli.py                 # CLI 入口
  config.py              # 配置加载 + runtime_state 读写
  db.py                  # 数据库操作（含 plans / operation_logs / templates / executions）
  importer.py            # CSV 导入
  merger.py              # 差异合并与查询
  plans.py               # 复核方案管理（CRUD + 落盘 + 筛选合并）
  templates.py           # 模板 CRUD + 版本化 + 导入导出
  batch.py               # 按模板批量执行 + 续跑
  archive.py             # 执行归档导出 / 恢复 + 冲突检测
  replay.py              # 操作日志回放 + 冲突检测
  reviewer.py            # 复核操作（状态、备注、撤销）
  exporter.py            # 报告导出（方案字段 + 一致性元数据）

tests/                   # 测试
  test_regression.py     # 原有回归测试
  test_plans_and_replay.py  # 方案 / 回放 / 冲突 / 重启续用 测试
  test_templates_and_batch.py  # 模板 / 批量执行 / 归档恢复 测试

samples/                 # 样例文件
  config.json            # 样例配置
  batch1.csv             # 第一批盘点样例
  batch2.csv             # 第二批盘点样例

audit_data/              # 运行时数据（自动创建）
  audit.db               # SQLite 数据库
  runtime_state.json     # active_plan + operator 持久化
  plans/                 # 方案 JSON 双写目录
    <方案名>.json
  templates/             # 模板 JSON 双写目录
    <模板名>.json
  exports/               # 导出报告 + 冲突快照
  archives/              # 执行归档清单（template-export-execution 默认输出目录）
```
