# Stage 3 手动组合回测调测

## 当前入口

`modules.strategy_hermes.manual_backtest` 提供 `run`、`show` 和 `copy-auto`。节点支持买入、加仓、减仓和卖出。金额、份额、现金比例和持仓比例按节点类型校验。同一场景各 ETF 共用现金，各 ETF 保留独立 FIFO 批次。买入与卖出沿用自动周期的 Decimal、费用、取价与 T+1 批次算法。

节点时间与成交时间分别保存。成交取节点之后第一条可用且交易日历允许的报价，且报价须在场景截止前已收到。T+1 部分可卖时先执行可卖份额，余量保留待执行。明确清仓覆盖未完成的同标的卖出意图。超出现金或未预留持仓的节点标记冲突，草稿、其他节点和可核对的试算明细一并保存。现金比例取录入节点时已发生的现金余额，成交时复核余额和成本。

场景和结果按输入、规则版本和父修订生成稳定标识。相同输入重复运行复用同一修订。复制自动周期在调测 PostgreSQL 中使用只读事务，保留来源周期、原订单和原始报价标识，编辑时重新从初始资金计算。自动周期与手动修订存放在不同位置。试算输入和结果保留价格模式。已显式标注的假设报价只进入假设场景。

## 调测命令

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"
APP_TMP_ROOT="$runtime/test-tmp" "$python" -B -m modules.strategy_hermes.manual_backtest \
  --state-dir "$runtime/stage3-manual" run tests/fixtures/stage3_manual_v14.json

"$python" -B -m modules.strategy_hermes.manual_backtest \
  --state-dir "$runtime/stage3-manual" show manual-eb693b99bc822ce175ed4613971ae37a

STAGE1_TEST_ROOT="$runtime/test-tmp/stage1-tests" \
STAGE2_TEST_ENV_FILE="$runtime/.env.test" \
OCTOP_DECISION_ACCEPTANCE_FILE="$runtime/stage2-derived/octop-direction-validation-8e3623bce23e1643f1117210.json" \
"$python" -B -m unittest discover -s tests -v
```

复制调测自动周期时，在同一命令入口使用 `copy-auto --env-file <调测环境文件> --input-kind FROZEN --auto-namespace <调测命名空间> <周期ID>`。

## 实际验收结果

- V1.4 假设价格样例从 10,000 元得到 10,950 元期末现金，毛利润 950 元，场景收益率 9.50%。原修订 `manual-eb693b99bc822ce175ed4613971ae37a`。
- 初始资金改为 12,000 元另存后，毛利润仍为 950 元，收益率为 7.9167%。新修订 `manual-ff76ecfa447e7bc42c0c7240790e9079`。原修订复读结果一致。
- 多 ETF 冻结样本中，旧仓 200 份当日卖出，目标剩余 300 份次日执行。次日到账资金用于另一只 ETF。
- 调测库复制自动周期，手动重放得到 1,000、500、500 份三笔成交，和原自动周期利润一致。复读自动账本内容一致。49 项回归测试通过。

企业行动数据待接入，当前结果明确标记分红及份额调整口径。

## 手动回测队列与命令鉴权

迁移 0008 新增 `manual_drafts` 和 `manual_jobs`。调测库与开发库均已验证。手动任务的草稿、运行结果和状态保存在各自命名空间，自动周期账本独立。队列当前每库最多 10 个待办任务和 1 个运行任务。修订由内容生成稳定标识，同修订重复提交返回同一任务，提交新修订可替代尚未开始的祖先任务。复制场景使用请求 ID 幂等。等待本地价格、取消、失败、成功各有状态。失败或取消的任务可显式提交重试。

在现有 Stage 2 `serve` 命令上增加 `--manual-queue` 启动。调度使用单独的监督线程，回放在一个低优先级子进程内执行，执行上限 120 秒，地址空间上限 512 MiB。取消会停止并回收子进程。任务租约与执行令牌拦截迟到结果。计算输入和输出通过标准流，结果写入数据库，运行过程无需产生临时文件。

调测 HTTP 入口绑定本机环回。配置服务账号可读的私有令牌文件，通过 `Authorization: Bearer <token>` 鉴权。接口为：

| 方法 | 路径 | 请求与结果 |
| --- | --- | --- |
| POST | /api/v2/backtests/manual | `bundle` 创建草稿，可传 `parent_id` 新建修订 |
| POST | /api/v2/backtests/{id}/run | 提交任务，`retry: true` 可重试失败或取消的任务 |
| POST | /api/v2/backtests/{id}/cancel | 取消排队或运行中的任务 |
| POST | /api/v2/backtests/{id}/clone | 用 `request_id` 幂等复制草稿 |
| GET | /api/v2/backtests/{id} | 读取已保存的草稿、状态及结果 |

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
"$runtime/stage2-venv/bin/python" -B -m modules.strategy_hermes.manual_api \
  --env-file "$runtime/.env.development" --namespace stock-v4:dev:manual-api-acceptance-20261112 \
  --token-file "$runtime/secrets/manual-api.token" --port 18083
```

真实调测通过 loopback HTTP 与现有 `serve --manual-queue` 验证。无令牌提交返回 401，创建草稿返回 201，提交返回 202，查询及重复提交返回 200。草稿 `manual-draft-38fa670fba1181f1dd24f9f53017faf8` 计算次数为 1，冻结样本收益率为 9.50%。并发验证在手动子进程仍运行时完成冻结 Hermes 轮次落库，随后取消回测并回收进程。队列满、缺价、过期任务和迟到提交均有回归测试。全套共 56 项通过。

## 三日历史触达

入口为 `python -B -m modules.strategy_hermes.three_day_touch --output-dir <持久目录> <本地证据包>`。证据包格式为 `stock-v4-three-day-touch-v1`，包含 `calendar`、`as_of`、`samples` 和 `prices`。样本保留原报告、发布时间、原提示、ETF 和来源。日线使用本地已保存的未复权开盘价与最高价，保留来源和可用时间。

同一报告、原提示、ETF 只计一次并合并来源。从报告之后下一交易日的开盘价起算，三日中任一最高价严格高于参考价即记触达。未满窗、缺交易日历和缺价格分开记录，触达率分母使用具备完整窗口价格的样本。输出是独立参考指标。冻结验收两个有效样本中一个触达，结果可重复读取 `/opt/workspace/stock-v4-runtime/stage3-touch/three-day-9f887c670d050a9bd3d08813.json`。

历史日线从调测环境已配置的 Tushare 采集。每个逻辑批次对单个 ETF 发出一次日期范围请求，保存未复权开盘、最高、最低和收盘价。相同批次重复执行直接读调测库。输入价格的可用时间使用实际采集时间。

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"
"$python" -B -m modules.strategy_hermes.stage2.etf_daily_history --env-file "$runtime/.env.test" collect \
  --config "$runtime/secrets/tushare-lianghua.json" \
  --calendar "$runtime/stage2-derived/calendar-729bbab2fc348decbf6af336.json" \
  --run-key stock-v4:test:etf-daily:20261110-20261112:512480:lianghua \
  --symbol 512480.SH --from 2026-11-10 --to 2026-11-12

"$python" -B -m modules.strategy_hermes.stage2.etf_daily_history --env-file "$runtime/.env.test" show \
  c2ab0727ea6a71e681a76bfab0f95e575090936d985bc4b2e38d6f93f6caedd7
```

2026 年 11 月 13 日调测采得 512480.SH 的三个完整交易日。11 月 10 日开盘 0.795 元，三个交易日最高价依次为 0.798、0.811、0.854 元。以标明为调测构造的报告信号调用计算入口，窗口为 11 月 10 日至 12 日，首次触达为 11 月 10 日。证据存放在 `/opt/workspace/stock-v4-runtime/stage3-touch/acceptance-only/three-day-7405bacb31817a1a13eebe7e.json`，用于调测验收。使用 11 月 13 日真实候选的验收窗口为 11 月 16、17、18 日，待 11 月 18 日收盘及完整日线入库后运行。线上鉴权配置待接入。


### 生产报告的三日触达

用生产 worker 环境导出已发布报告的真实向好候选。保留原提示、映射版本和原报告散列。可把导出经标准输入送至调测机，使用同一入口的 `evaluate` 和已入调测库的 `fund_daily` 批次，结果通过 `temp_manager` 存入证据目录。

```bash
ssh -T tencent 'cat /opt/workspace/stock-v4/tools/three_day_live_evidence.py' |
  ssh -T telestock 'docker exec -i stock-v4-worker python -B - export --day 2026-11-13'
```

对每只实际出现的 ETF，在 11 月 18 日收盘并可获取完整日线后，通过本节 `etf_daily_history collect` 一次查询 11 月 16–18 日。将各批次通过 `--batch` 传给 `tools.three_day_live_evidence evaluate`。尚在观察期的有效样本标为 `WAIT_WINDOW`，完整窗口缺价标为 `MISSING_PRICE`。没有合格信号时返回 `NO_ELIGIBLE_SAMPLES`。

## ETF 分红与份额事件

在 `stock-v4-manual-v1` 历史证据包的顶层可填写 `corporate_actions`。每项提供 `id`、`etf`、`source`、带时区的 `published_at` 和事件类型 `kind`。

- `CASH_DIVIDEND` 使用 `record_date`、`ex_date`、`pay_date`、人民币 `cash_per_share_cny`。登记日收盘确定权利，除息日起单列应收，派息日记入现金和已实现收益。交易成交仍按导入的原始可成交报价。
- `SHARE_SPLIT` 使用 `effective_date` 和整数 `numerator`、`denominator`。逐批调整份额、保留总成本和解锁日。含零碎份额的转换需要结算规则证据，当前报错。跨转换的未完成订单需在换算后重新录入。
- 含事件的回放采用 `manual_portfolio_v2`，仅计算证据包提供的事件。原有回放保持原版本。2026-11-13 调测机用 Tushare 中转对 512800.SH 单次查询 `fund_div` 返回空结果。当前仍通过导入的证据包登记，供应商自动归集待数据覆盖验证。
