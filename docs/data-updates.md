# 原模板：数据更新与补采

2026-11-14，按用户确认完整恢复原管理页面和五类补采。依据为保留的 `AdminDataUpdates.vue`、`tokens.css`、`shell.css`、`admin.css`，以及原 `tushare_eod.py`、`tushare_reference.py`、`tushare_policies.py`、`eod_policy.py`。研究区恢复原白顶栏、232px 侧栏与紧凑表格；管理区恢复原 1280px 容器和 220px 侧栏。

## 使用

进入 **系统管理 → 数据更新与补采**。按原页面选择周期、填写范围并点“提交更新任务”，后台执行，可离开页面。“刷新状态”只读取本地数据，状态表保留业务日期、覆盖范围、原始观察时间、返回行数和错误；最近任务提供原“全部采集与任务”链接。

| 周期 | 原范围与执行内容 |
| --- | --- |
| 日线与指标 | 最多31个自然日内已结束交易日；按日全市场采 daily、adj_factor、daily_basic、cyq_perf，随后生成对应日期 LR 盘后档案 |
| 周线补采 | 指定日期范围内已经完整结束的交易周，独立 weekly 采集 |
| 基础资料 | stock_basic 的 L/D/P，SSE/SZSE 交易日历（提交年度前一年至后一年），最新申万行业 index_member_all |
| 月更 | 选定月份的 dc_member 已完成交易日快照及 share_float 整月解禁范围 |
| 年更 | 已完成报告年度的 fina_indicator_vip，成功后按最新已存日线日期重算策略 |

空库先补基础资料和周线，再按日期范围分批补日线。新页面日线仅采所选日期，不自动扩大日线历史范围；评分需要的历史长度沿用原公式。LR 读取已保存资料按50只股票分块计算，缺数保留具体原因。日更依赖原 Kaipanla 板块历史及成员采集器，范围为首个目标日前4个交易日至末日的042，以及首个目标日前一日的046；使用原完整分页和历史成员规则。个别源失败保留其失败状态，其余已保存输入仍交原评分器计算。

五类更新保留6小时来源缓存；成功页可复用，缓存保留原采集时间，过期可接收供应商修订。Tushare 日/周线每页3000、最多10页；参考资料每页4000、最多100页，申万每页2000。日线股数/金额/涨跌幅按原单位转换；兼容路由周线保持股／元／比例。解禁合法空范围显示 EMPTY 和0行，其余必须有数据的接口空结果显示失败。

原次晨同步由现有唯一 worker 在北京时间06:00至08:30窗口提交最近业务日，每业务日唯一任务键。启动或排队延后仍可在窗口内提交，失败任务由手动补采处理；06:00前、08:30后不会因启动触发补采。自动提交服从生产环境、官方采集和禁外部请求开关。现有 Hermes 五轮调度、QQ 和盘后参考规则沿用。

## 代码与接口

- `web/data-updates.js/css`：原五周期表单、同步状态分布、最近任务和任务分页；`web/template-layout.js/css`：原模板外壳。
- `modules/strategy_hermes/data_updates.py`：范围计划、持久逻辑任务、原快照与任务API、盘后依赖和晨间调度。
- `modules/strategy_hermes/data_updates_collect.py`：五类源合同、缓存、批量存储和来源回执。
- `modules/strategy_hermes/left_right_backfill.py`：共享有界队列、唯一worker、请求预算、暂停和租约；旧接口继续兼容。
- `modules/strategy_hermes/stage2/migrations/0012.sql`：参考资料、逻辑任务及有效空批次；0011历史记录保留。
- `GET /api/v2/workspace/data-updates`：11类源状态、最近任务、最新业务日。
- `POST /api/v2/workspace/data-updates`：`{mode,date_from,date_to}`、`{mode,month}`、`{mode,year}`或基础资料`{mode:"basic"}`；隐藏`request_id`用于幂等，返回202及任务数。
- `GET /api/v2/workspace/data-update-jobs?page=1&page_size=50`：任务表分页。

所有数据接口鉴权；重复提交复用同一任务，参数冲突409，队列满429，发布暂停503。请求与业务状态持久保存，状态提交核对当前租约。表单控件遵照原模板，内部预算沿用现有队列：3组待完成任务、全局1组执行、每组12000次请求和90分钟。

## 运行与验证

```bash
python -B -m modules.strategy_hermes.runtime migrate
python -B -m modules.strategy_hermes.runtime api
python -B -m modules.strategy_hermes.runtime worker

# 使用独立空测试库，先执行有数据升级测试，再运行全套。
STAGE2_MIGRATION_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -p test_migration_upgrade.py -v
STAGE2_TEST_ENV_FILE=/absolute/private/test.env \
  python -B -m unittest discover -s tests -v
python -B -m modules.strategy_hermes.runtime smoke --offline
node --check web/app.js
node --check web/data-updates.js
node --check web/template-layout.js
```

本次验证使用隔离 PostgreSQL/Redis 与供应商 HTTP 替身，实际手动历史补采留给用户在执行服务器提交。现有正式 LR 历史仍为已保存的11月11日、12日少量档案。LEFT/RIGHT沿用盘后模型；本次未引入实时LR或下单功能。

调测实测：全套360项运行183.182秒，359项通过，迁移项在全套中跳过并在前置空库独立通过，合计360个独立用例通过。0011→0012保留已有周期、回测、成交和旧800请求预算任务。6000只股票生成12000条评分耗时26.912秒；冻结回放收益保持9.5000%。原页面合成测试覆盖五类精确提交、幂等重试、状态和分页；真实调测桌面1440／手机390验收无浏览器错误或整页溢出，业务POST为0。

发布前追加真实两连接竞争验证：逻辑任务状态写入与父任务行锁在同一事务内完成；取消操作等待该事务提交。22项数据更新专项全通过。进程内移除该事务的负对照稳定复现竞态失败，修复版通过；最终总用例数361项。
