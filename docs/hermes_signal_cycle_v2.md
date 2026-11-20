# hermes_signal_cycle_v2 退出规则

2026-11-20 用户授权（`CLAUDE.md` 第 31 条）。v2 只改变退出，不改入场、OCTOP 提示词和报告格式；
已开的 v1 周期继续按冻结的 v1 规则运行。

## 代码入口

| 位置 | 作用 |
|---|---|
| `modules/strategy_hermes/simulator.py` | `Rules` 的 v2 字段、`EXIT_TABLE`、`Rules.v2(regime_mode)`、`Rules.snapshot()` |
| `modules/strategy_hermes/signal_cycle.py` | `process_quote()`、`build_exit_plan()`、`evaluate_exit()`、`apply_report()` 的 v2 分支 |
| `modules/strategy_hermes/ledger.py` | 报价改走 `process_quote()`；`_exit_inputs()` 基类返回空（两个回退标注） |
| `modules/strategy_hermes/stage2/postgres.py` | `_exit_inputs()` 读取已入库的 107 指数与 ETF 统计；`_rules()` 按配置取新周期规则 |
| `modules/strategy_hermes/stage2/settings.py` | `MODEL_EVALUATION_RULE`、`REGIME_MODE` |
| `modules/strategy_hermes/stage2/market_regime.py` | 107 规范化、`stored_series()`、`classify()`、`recent()` |
| `modules/strategy_hermes/stage2/etf_statistics.py` | `atr20()`；统计保留高低价、24 只、25 个交易日 |
| `modules/strategy_hermes/stage2/macro_refresh.py` | 107 并入大盘全景采集：每个工作日 15:05 后每个指数一次 |

## 规则

买入成交的那条报价生成并冻结 `exit_plan`（之后只读）。此后每条有效报价先撮合已有订单，再按顺序判定，取第一条命中：

| 顺序 | 条件 | 动作与原因码 |
|---|---|---|
| P1 | 浮盈 ≤ −止损线 | 全清 `STOP_LOSS_HIT` |
| P2 | 已有成交的减仓，且 `breakeven_armed`，且价格 ≤ 成本 × 1.003 | 全清 `BREAKEVEN_PROTECT` |
| P3 | 采样峰值浮盈 ≥ 启动线，且自峰值回撤 ≥ 回撤线 | 全清 `TRAILING_TAKE_PROFIT` |
| P4 | 报价时刻 ≥ 14:05：持有交易日 ≥ 上限；或 ≥ 滞涨天数且浮盈 < 滞涨线 | 全清 `TIME_EXIT_MAX_DAYS` / `TIME_EXIT_STAGNANT` |
| P6 | 未减仓、无待成交订单，浮盈 ≥ 目标线 | 减半 `TARGET_PROFIT_REDUCE`，并武装保本 |

* 峰值浮盈 ≥ +1.5% 或目标止盈减半后 `breakeven_armed = True`，周期内不回退。
* 持有交易日从买入成交日起算（成交日为 0）。
* 触发全清时，未成交的减仓单先置 `SUPERSEDED`；可成交的减仓单先在同一报价成交，全清只针对剩余份额。
* 刚开仓、刚清仓的那条报价不判规则。新订单由之后的报价成交，T+1 规则不变。
* `WAIT_EXIT` 之后不再判规则，报告也不再改订单。

报告（P5）：明确退潮全清 `AI_RETREAT`；`WEAK` 按交易日去重计数，首次减半 `AI_FIRST_WEAK_REDUCE`，
跨交易日第 2 次全清 `AI_WEAK_CROSS_DAY_2`；`HOLD`/`UP` 清零，`UNKNOWN` 或板块缺席不变。
`bear_early_weak_exit` 默认关闭；开启后弱势档、未武装、成交次日起、当日最近一条报价低于成本时首次 `WEAK` 直接全清
`BEAR_EARLY_WEAK_EXIT`。

### 三档参数

各线 = `clamp(倍数 × ATR20, 下限, 上限)`。

| | `BEAR_DEFENSE` | `RANGE_ROTATION` | `BULL_OFFENSE` |
|---|---|---|---|
| 止损线 | 0.9, 2.5%, 4.0% | 1.0, 3.0%, 5.0% | 1.1, 3.0%, 5.5% |
| 移动止盈启动线 | 0.40, 2.0%, 3.0% | 0.50, 2.5%, 3.5% | 0.60, 3.0%, 4.5% |
| 自峰值回撤线 | 0.35, 1.2%, 2.0% | 0.40, 1.5%, 2.5% | 0.50, 2.0%, 3.0% |
| 目标止盈线 | 0.70, 2.5%, 3.5% | 0.80, 3.0%, 4.5% | 1.00, 4.0%, 5.5% |
| 滞涨退出 | T+2 且 < +1.0% | T+3 且 < +1.0% | T+4 且 < +1.5% |
| 持有上限 | T+3 | T+5 | T+7 |

### 档位与 ATR20

* `T-1` 为买入成交日前一个交易日。`DUAL` 用沪深 300（`SH000300`）和中证 1000（`SH000852`），`SINGLE` 用上证指数。
* 强势：各指数收盘高于 MA20、MA20 高于 5 个交易日前，且量能比（近 5 日均额 / 近 20 日均额）≥ 1.10。
  弱势：各指数收盘低于 MA20，且任一 MA20 低于 5 个交易日前或量能比 ≤ 0.90。其余为震荡。
* 库里最新一根已收盘 K 线不是 `T-1`，或不足 25 根：回退震荡档并标注 `REGIME_FALLBACK_RANGE`。
* ATR20 = 成交日前 20 个交易日 `max(high−low, |high−pre_close|, |low−pre_close|) / pre_close` 的均值，只用该 ETF 的统计行。
  允许窗口截至 `T-3` 以内并记录 `atr_as_of`；否则 `atr20 = null`、标注 `ATR_UNAVAILABLE`，各线取下限。
* 以上数值写入周期 `body.exit_plan`，不进入 `rule_versions`（每个版本只有一份快照）。

## 配置

| 配置 | 取值 |
|---|---|
| `MODEL_EVALUATION_RULE` | `hermes_signal_cycle_v1`（未设置时的默认）或 `hermes_signal_cycle_v2`；其他值启动失败 |
| `REGIME_MODE` | `DUAL` 或 `SINGLE`；选择 v2 时必填。2026-11-20 已验证 107 支持沪深 300 与中证 1000，使用 `DUAL` |
| `APP_KAIPANLA_CAPTURES` 目录 | 增加一份 `107_*.json` 私有请求配置，不进 Git |

该开关只决定**新建**周期的规则。回退：改回 v1 并重启，已开的 v2 周期按自身冻结规则跑完，无需数据库操作。
修改 `Rules.v2()` 的任何数值都必须换新的版本名，否则报 `changed rules require a new version`。
v1 的规则快照不含 v2 字段，与升级前逐字节一致。

## 展示

| 位置 | 内容 |
|---|---|
| 大盘全景 · 大盘三档（`market_panorama.load_regime`、`web/app.js` 的 `renderMarketRegime`） | 近 5 个交易日收盘判定的档位折线图，每日附量能比和各指数相对 MA20 的位置。只读已入库的 107 日线；数据不足的交易日显示“数据不足”并画一条虚线，不画档位，也不与相邻日连线 |
| 工作台 · 退出计划 | 规则版本、大盘档位、ATR20、止损线、移动止盈、目标止盈、采样浮盈与峰值、余仓保本状态、持有日 / 上限、回退标注（`signal_cycle.view` 的 `exit_view`） |
| 工作台 · 自动节点 | 触发原因、触发价、成交价 |
| QQ 轮次简报（`stage2/notifications.py`） | 节点按原因码显示（如“止损清”“目标止盈减”）并带触发价；由报价触发的节点标“报价触发”，上一份报告之后才触发且已成交的也会列出一次 |

浮盈与峰值只取轮次采样报价，不用日线最高价。

## 影子回放

`tools/shadow_exit_v2.py` 只读数据库：把每个已成交买入的自动周期用同一份入场报告、同一条买入报价重建，
再把之后保存的报告和报价按账本顺序喂给 v2 引擎，输出记录结果与 v2 模拟结果的对比，不写任何账本行。

```bash
python -B -m tools.shadow_exit_v2 --env-file /absolute/.env --since 2026-11-13
docker exec -i stock-v4-backend python - < tools/shadow_exit_v2.py     # 镜像内没有 tools/，脚本从标准输入传入
```

* 默认按买入成交时刻已入库的 107 指数和 ETF 统计取档位与 ATR20，取不到就回退并计入 `fallback_flags`。
  `--inputs plans.json`（`{"512480.SH": {"market_regime": "BEAR_DEFENSE", "atr20": "0.0551"}}`）用给定值代替，
  用于回放采集上线之前的周期。
* 两边都按该 ETF 最新一条有效报价估值；`summary` 给出已成交退出的原因码计数、回退标注次数、
  成交价相对触发价的平均偏离、已清仓周期的平均持有交易日、收益率差的均值。
* 模拟周期重新走买入撮合，入场准入、资金规则与记录周期相同；记录周期里人工或准入造成的差异不会被复现。

## 运行与测试

```bash
python -B -m unittest tests.test_signal_cycle_v2 tests.test_market_regime -q
python -B -m unittest discover -s tests -q   # 全量，需按 README 先起 PostgreSQL 和 Redis，不并行
```

`tests/test_signal_cycle_v2.py` 的用例名带开发文档的编号（T01–T27）；T15 的分档和 T26 的采集调度在
`tests/test_market_regime.py`。展示相关用例在 `tests/test_market_panorama.py`（`RegimeTest`）、
`tests/test_qq_visual_report.py` 和 `tests/test_stage2_postgres.py`（`test_v2_exit_raised_and_filled_between_rounds…`）。

## 未实现

* T12（用执行服务器 11/13–11/20 的真实报告与报价回放三个周期）未做成自动化用例：仓库里没有这批生产数据。
* `invalidation_condition` 未接入。
