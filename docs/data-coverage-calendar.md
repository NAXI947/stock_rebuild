# 数据覆盖日历

在“数据更新与补采”原表单下方增加只读日历，按月份查看历史入库情况。原日线与指标、周线、基础资料、月更、年更五类补采及每次最多 31 个自然日的范围约束保持原样。

## 数据与状态契约

认证后调用 `GET /api/v2/workspace/data-coverage?month=YYYY-MM&view=daily`。

- `month` 接受 `0002-01` 至 `9998-12`；省略时使用北京时间当前月。
- `view` 为 `daily`、`weekly` 或 `assessment`；省略时为 `daily`。非法参数返回 `400 INVALID_COVERAGE_QUERY`。
- 返回 `month`、`view`、`latest_business_date`、`summary` 和 `days`。汇总只计 `AVAILABLE`、`PARTIAL`、`MISSING` 的天数。
- 每日包含 `date`、`is_open`、`is_week_end`、`status`、`available_count`、`expected_count` 和 `datasets`。数据项包含名称、状态、`row_count`、`ready_count`、`source_at`、`updated_at`。

| 视图 | 适用数据与判定 |
| --- | --- |
| 日线与指标 | 日线、复权因子、每日指标、筹码四项均有入库记录为有数据；仅部分有记录为部分缺项。 |
| 周线 | 记录到该交易周最后一个实际交易日；其他交易日显示非周结日。 |
| 盘后评估 | 查看 LEFT/RIGHT 评估归档；已有条数少于当日日线条数 × 2 时显示归档未齐。 |

“有数据”采用数据集入库存在性口径。休市、未来待更新、交易日历未知、非周结日分别显示，均不计入无数据。盘后完成时间沿用北京时间 **15:35**：已开市日期到此时才开始要求应有数据；已实际入库的记录直接展示。此时间是日历状态的业务日期界线，采集任务仍按原有调度执行。

日期详情固定显示四项日频行情、周线和盘后评估，再附上当天实际存在的基础资料、月更、年更记录；低频资料不要求逐日齐全。`AVAILABLE` 表示有实际记录；`EMPTY` 表示已确认的有效空结果；`MISSING` 表示适用日期缺记录；`NOT_DUE` 表示本日不适用。

LEFT/RIGHT 的 `row_count` 是归档条数，`ready_count` 是其中状态为 `READY` 的可评分条数。历史支撑不足时可出现“已归档、可评分 0 条”；已有归档仍保留可见。归档未齐时显示实际条数及按当日日线计算的应归档量，避免用 `1 / 1` 数据集比值表达归档完整度。

## 持久化与增量更新

迁移 `0013.sql` 创建 `data_coverage`，按命名空间、数据集、业务日期保存计数和时间，历史记录无过期清理。迁移时从已发布业务事实一次性初始化；之后使用 PostgreSQL 语句级触发器，依据本次增删改的变更行增量更新，与业务数据在同一事务中提交或回滚。评估记录按运行命名空间隔离。

旧版显式股票代码补采已保存的规范 JSON 也在升级时投影到共享特征事实表；后续同类批次提交时同步投影。原字段、单位和策略公式保持一致，较新的观测优先，同一采集时间保留已有规范表数据，完整周线才计入周线覆盖。

成功补采或重算后同一日期覆盖记录随之更新；未完成任务和原始供应商分页不提前记为已有数据。接口只读取覆盖表和交易日历，不扫描历史行情明细，不调用供应商，不创建采集任务，刷新也不改记录时间。

## 实现入口

- `modules/strategy_hermes/stage2/migrations/0013.sql`：表、历史初始化、增量触发器。
- `modules/strategy_hermes/data_coverage.py`：查询验证、月份读取、交易日与三视图状态。
- `modules/strategy_hermes/workspace_web.py`：认证后的 GET 路由。
- `web/data-updates.js`、`web/data-updates.css`：原页面内的日历；周一开头，月份导航、独立视图筛选、日期明细、键盘方向键、失败重试和过期响应保护。
- `tests/test_data_coverage.py`、`tests/test_data_coverage_storage.py`、`tests/test_data_coverage_ui.mjs`：语义/API、存储和前端交互回归。
- `.github/workflows/verify-build.yml`：前端检查步骤执行 Node 交互测试；现有 Python 测试发现步骤覆盖后端与存储测试。

## 运行与测试

应用迁移后沿用现有后端和 Web 启动方式，进入 `#data-updates` 页面。迁移命令在正确运行环境中执行：

```sh
python -B -m modules.strategy_hermes.runtime migrate
```

前端验证不访问外部服务：

```sh
node --check web/data-updates.js
node --test tests/test_data_coverage_ui.mjs
```

后端测试使用新建的、可丢弃的独立测试库，初始不得存在 `stock_v4_stage2` schema。环境文件包含 `APP_ENV=test`、测试数据库和 Redis 配置。先运行迁移升级测试，再在同一测试库运行覆盖测试；不要先对该测试库执行运行环境迁移或覆盖测试，否则迁移升级测试的空库前置条件会失败。以下为 POSIX shell 命令：

```sh
STAGE2_MIGRATION_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -p test_migration_upgrade.py -v
STAGE2_TEST_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -p 'test_data_coverage*.py' -v
```

未提供测试环境文件时，仅运行纯状态语义测试，数据库用例会标记跳过。调测服务器验收完成后，按现有同版本镜像发布流程推送执行服务器。

## 本次代码验证记录

2026-11-16，调测服务器在独立、无外网的 PostgreSQL 16 / Redis 7 环境中先执行有数据迁移升级测试（1 项通过），再执行 `python -B -m unittest discover -s tests -v`：382 项，185.925 秒，381 项通过，已单独执行的迁移测试在常规发现中跳过。离线运行冒烟通过，数据库修订为 0013。6000 只股票的 12000 条归档、分页和板块读取回归耗时 19.226 秒。

本机 Node 的语法检查和 3 项日历交互测试通过，CI 同样执行这些检查。浏览器已检查桌面与 390 像素窄屏，月份、三视图、日期明细及七列布局可用。本次日历功能范围内无待实现项。
