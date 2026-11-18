# LEFT/RIGHT、热门板块与手动历史补采

2026-11-14：在现有 Stage 4 工作台增加两个页面，沿用前端备份的家族筛选、日期、股票详情、多日板块列和曲线布局。

## 使用

当前进入 **系统管理 → 数据更新与补采**，按原模板使用日线与指标、周线、基础资料、月更、年更五类更新。日线按选定日期批量采集全市场，随后生成对应日期的 LEFT/RIGHT 盘后档案。日期范围最多 31 个自然日。完整操作与本次恢复记录见 [数据更新与补采](data-updates.md)。

提交后后台处理；离开页面不影响任务。页面读取和刷新只读取数据库、已有归档。任务队列最多容纳 3 个未结束任务，同一环境数据库同时执行 1 个；全市场单任务最多 12000 次外部请求、90 分钟，已有匹配资料优先复用。暂停和取消在当前有界请求/数据库步骤之后生效，失败或租约中断不会自动重新发出不确定的供应商请求。

热门板块默认展示最近 5 个已保存交易日、每日前 10 名，可扩展到 20 日/50 名。最新交易日及当日仅展示板块。其他日期的股票直接读取该评分日 LR 归档：LEFT ≥75、RIGHT ≥70，各最多 5 只。成员名单使用评分日及之前最近的完整批次，卡片显示实际成员日期。

## 实际入口与数据约定

| 入口 | 职责 |
| --- | --- |
| `web/app.js`、`web/index.html`、`web/style.css` | 两个研究页面及股票详情 |
| `web/data-updates.js/css`、`web/template-layout.js/css` | 原管理补采页面、状态表和原模板外壳 |
| `modules/strategy_hermes/left_right_info.py` | 只读历史评分，最多 120 日；日期/家族/搜索/分页 |
| `modules/strategy_hermes/hot_sectors_info.py` | 已保存 041 榜单、042 历史收盘榜单及历史 LR 关联 |
| `modules/strategy_hermes/left_right_backfill.py` | 持久队列、幂等、预算、取消/暂停/租约及 worker 执行器 |
| `modules/strategy_hermes/bulk_eod_collect.py`、`left_right_backfill_collectors.py` | 按交易日全市场分页采集、成功页缓存、共享资料入库及开盘啦历史采集 |
| `modules/strategy_hermes/stage2/left_right_eod.py` | 原 `left_v3.1.2_adj` / `right_v3.1.2_adj` 计算及输入适配 |
| `modules/strategy_hermes/stage2/left_right_refresh.py` | `build_manual_archives`、`persist_archive`，按日及股票/方向保存 |
| `modules/strategy_hermes/stage2/migrations/0010.sql`、`0011.sql` | 任务/请求记录、成功分页/完整日清单、索引特征与评分记录 |

所有 API 均在 `/api/v2/workspace` 下鉴权：

- `GET /left-right-info?family=RIGHT&trade_date=2026-11-12&q=&page=1&page_size=20`
- `GET /hot-sectors-info?days=5&limit=10&q=`
- `GET /left-right-backfill`：最近 20 个任务及限制。
- `POST /left-right-backfill`：`{request_id,mode,scope:"ALL",date_from,date_to}`；mode 为 `collect` 或 `recompute`。保留旧 API 兼容；当前 Web 使用原五类 `/data-updates` 入口。
- `POST /left-right-backfill/cancel`：`{job_id}`。

重复 request_id/相同参数返回同一任务，参数冲突返回 409，满队列 429。未登录 401，发布暂停时新增任务 503。HTTP 写入口仅入队，采集在唯一 worker 的有界执行线程内完成。

旧兼容接口补采先取日期范围前 280 个自然日的真实日历。daily、adj_factor、daily_basic、cyq_perf 逐交易日请求全市场，weekly 只取已完整结束的交易周。每页 3000 条、每接口每日最多 10 页，核对日期、字段和分页重复；末页不足 3000 条才完成该日数据。成功分页在独立事务中保存，6 小时内复用；已完整入库的匹配日批次长期复用，保留原采集时间。失败后手动提交新任务继续补齐缺失页。周线保留旧版本原始 close 口径，使用完整交易日历判定完整周；日线复权和评分公式保持不变。LEFT 的五日板块收益采用原日收益复利与前日板块选择规则。042 固定 `Type=1,Order=1,RStart=0925,REnd=1500`，和 041 强度榜口径一致，榜单保留供应商原始排名及分页缺口；046 只采用有明确历史日期且分页完整的成员名单。

逐条结果保存真实评分日期、资料可用时间、来源批次及证据哈希；部分股票重算保留其他股票原记录。缺少必要输入的结果保留 MISSING 和具体原因。全市场评分按 50 只股票分块计算，写入 `left_right_assessments` 和板块关联表，历史页在 SQL 中筛选与分页；旧少量档案继续兼容 `snapshot_state`。任务进度按采集/重算阶段记录真实日期、请求数、复用页数及处理股票数；累计缺失数与最多 100 条明细分开保存。旧兼容接口仅响应手动提交；恢复后的原五类页面及次晨调度见 data-updates.md。

## 配置与运行

现有 worker 入口会启动补采执行器：

```bash
python -B -m modules.strategy_hermes.runtime migrate
python -B -m modules.strategy_hermes.runtime api
python -B -m modules.strategy_hermes.runtime worker
```

运行环境提供原有数据库、缓存、工作台鉴权和交易日历配置。新增读取配置：

- `APP_LEFT_RIGHT_HISTORY_FILE`，默认 `/app/data/left-right-history.json`。
- `APP_HOT_SECTOR_HISTORY_FILE`，默认 `/app/data/hot-sector-history.json`。
- `APP_TUSHARE_CONFIG`，默认 `/app/automation/tushare-eod.json`，私密文件权限 0600，worker 用户可读。
- 原 `APP_KAIPANLA_CAPTURES` 目录需要增加 `042_*.json`、`046_*.json` 私密捕获配置。

手动外部采集只在生产 worker 且 `DISABLE_EXTERNAL_PROVIDER_HTTP` 未开启时执行。仅重算读取已有资料。发布保留已有五轮 Hermes、QQ 和行情采集配置。

全市场采集依据 `backend/app/providers/tushare_eod.py` 的 `trade_date` 批量分页和 `tushare_shared.py` 的成功分页缓存，不采用逐股五接口请求方式。

备份依据：`StrategyPicksView.vue`、`SectorRotationBoard.vue`、`AdminDataUpdates.vue`；历史成员低频复用来自 `backend/app/application/personal_discovery.py:123` 的 `Date <= trade_date` 规则。

## 前一版批量引擎验证记录

调测服务器使用 network-none PostgreSQL/Redis 隔离实例，外部行情禁用：

```bash
STAGE2_MIGRATION_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -p test_migration_upgrade.py -v
STAGE2_TEST_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -v
python -B -m modules.strategy_hermes.runtime smoke --offline
node --check web/app.js
```

全套运行 321 项，320 项通过、迁移项在全套中跳过；迁移项已在前置空隔离库独立通过，合计 321 项独立用例验证。升级测试从有数据的 0001–0010 迁到 0011，保留历史周期、手动回测、实际成交及旧补采任务。冒烟回放收益仍为冻结样例 9.5000%。

全市场完整链路用模拟 HTTP 和真实 PostgreSQL 验证：按日采集产生 851 次物理请求，280 自然日预热资料经原计算器生成两侧 READY、任务 SUCCEEDED，SQL 归档与分页正确；第二次同范围任务新增物理请求为 0。分页专项 10 项通过，覆盖 3001 行分页、失败页补采、业务事务真实回滚后成功页复用、字段/日期/重复页拒绝、空末页和 6 小时过期。

规模测试使用冻结评分输入和真实 PostgreSQL，6000 只股票按 50 只分块生成 12000 条评分；验证每页 100 条、每板块每侧前 5 条、单股重算保留其他记录、取消前检查及缺失摘要上限。调测本轮计算与归档验证耗时约 29 秒；实际历史补采还包含供应商请求与输入读取。每条评分的证据哈希对应持久来源清单 `snapshot_state/left_right_evidence:<hash>`，与结果同事务保存。

队列/API 测试同时验证跨暂停累计时间预算、取消、外部调用禁用、失败请求预算、幂等、租约、发布暂停和鉴权。

实际只读验证：两个交易日各 243 个板块；11 月 12 日银行排名 37，关联平安银行 RIGHT 76.706；最新交易日无股票关联。桌面 1440px、手机 390px 检查历史详情、全市场表单、查询、曲线、任务 GET 刷新，无横向溢出或浏览器错误，实际补采 POST 为 0。

现有正式历史资料只有 11 月 11 日、12 日少量 LR 档案；LEFT 原档案仍因资料不足标 MISSING。部署提供手动入口与所需配置，完整历史由用户后续在执行服务器提交补采。本轮未执行真实补采，也未恢复实时 LR。
