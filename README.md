# inventory-audit

盘点差异复核 CLI 工具：导入盘点 CSV、合并差异、复核（状态/备注/撤销）、按方案导出报表，
支持模板化批量执行、操作日志回放，以及**整段审计会话的打包归档与跨环境恢复**。

## 功能概览

- **导入与合并**：`init` / `import` / `list` / `show` / `remerge`
- **复核操作**：`status` / `remark` / `undo` / `history`（状态变更写入操作日志，可撤销）
- **方案管理**：`plan-save` / `plan-list` / `plan-use` / `plan-delete`（驱动导出字段与筛选）
- **报告导出**：`export -t differences|summary|sources`（带一致性元数据）
- **模板与批量执行**：`template-*` 系列命令（版本化模板、批量执行、中断续跑）
- **执行归档（单次）**：`template-export-execution` / `template-restore-execution`（按模板执行清单迁移）
- **审计会话归档（整段）**：`session-archive-create` / `-info` / `-restore`（整段会话打包 zip，跨机器迁移）

## 安装与运行

```bash
# 直接以模块方式运行（无需安装）
python -m inventory_audit -c config.json init
```

配置文件为 JSON 格式，样例见 [samples/config.json](samples/config.json)。
完整命令与参数说明见 [USAGE.md](USAGE.md)。

## 快速开始

```bash
# 1. 初始化数据库
python -m inventory_audit -c config.json init

# 2. 导入盘点数据
python -m inventory_audit -c config.json import samples/batch1.csv -n 2024-01-上午盘

# 3. 查看差异并复核
python -m inventory_audit -c config.json list
python -m inventory_audit -c config.json show 1
python -m inventory_audit -c config.json status 1 confirmed
python -m inventory_audit -c config.json remark 1 "需复盘"

# 4. 导出报告
python -m inventory_audit -c config.json export -t differences
```

## 审计会话归档与迁移

把当前会话（数据库 + 配置 + 导出报表 + 操作日志 + 运行时状态）整体打包，迁到新环境后恢复继续工作：

```bash
# 源环境：打包整段会话为 zip
python -m inventory_audit -c config.json session-archive-create
#   -> audit_data/archives/session_<操作人>_<时间戳>.zip

# 查看归档内容（不恢复）
python -m inventory_audit session-archive-info session_tester_20240101_120000.zip

# 恢复到新工作目录（遇到已有数据库/同名配置时绝不静默覆盖）
python -m inventory_audit -c config.json session-archive-restore \
    session_tester_20240101_120000.zip \
    --target-dir /path/to/new_workspace \
    --conflict abort|rename|overwrite

# 在新工作目录继续工作
cd /path/to/new_workspace
python -m inventory_audit -c config.json list
python -m inventory_audit -c config.json export -t differences
python -m inventory_audit -c config.json undo
```

冲突策略 `--conflict`：

| 策略 | 行为 |
|------|------|
| `abort`（默认） | 检测到已有数据库或同名配置立即中止，不改动任何数据 |
| `rename` | 恢复到 `audit_data_restored/` 与 `config_restored.json`，保留现有数据 |
| `overwrite` | 直接覆盖已有数据库与配置文件 |

恢复前会做 sha256 完整性预校验；归档损坏 / 版本不兼容 / 路径不可写等失败场景均有明确错误提示。
创建与恢复两个关键动作都会写入 `operation_logs`（`session_archive` / `session_restore`）。

## 测试

```bash
python -m unittest discover -s tests -v
```

会话归档相关回归测试见 [tests/test_session_archive.py](tests/test_session_archive.py)，
覆盖归档+恢复往返、三种冲突策略、损坏/版本/完整性校验失败、以及恢复后继续 list/export/undo 的跨重启一致性。

## 目录结构

```
inventory_audit/         # 主包
  cli.py                 # CLI 入口
  config.py              # 配置加载 + runtime_state 读写
  db.py                  # 数据库操作（plans / operation_logs / templates / executions）
  importer.py            # CSV 导入
  merger.py              # 差异合并与查询
  plans.py               # 复核方案管理
  templates.py           # 模板 CRUD + 版本化
  batch.py / batch_templates.py  # 按模板批量执行 + 续跑
  archive.py             # 单次模板执行清单（导出/恢复）
  session_archive.py     # 审计会话归档包（整段会话打包 zip + 恢复）
  replay.py              # 操作日志回放 + 冲突检测
  reviewer.py            # 复核操作（状态、备注、撤销）
  exporter.py            # 报告导出
tests/                   # 测试
samples/                 # 样例配置与盘点 CSV
audit_data/              # 运行时数据（自动创建）
```

详见 [USAGE.md](USAGE.md)。
