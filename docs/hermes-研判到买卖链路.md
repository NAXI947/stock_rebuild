# Hermes 研判 → 板块 → ETF 穿透 → 买卖：当前生效链路

整理日期 2026-11-21，对应 `ETFv2.0`（`main` 提交 `8eb0720`）和执行服务器上部署的 OCTOP skill
`a-share-hotspot-scout`（与仓库 `skills/a-share-hotspot-scout/` 逐字节相同）。本文只描述代码现在怎么做，
用来审核 AI 报告和买卖逻辑是否对得上；每一节末尾列出代码入口，第 7 节列出对照时值得看的地方。

规则授权见 `CLAUDE.md` 第 26–33 条。分项细节：[退出规则 v2](hermes_signal_cycle_v2.md)、
[方向与执行统一整改](etf-direction-unified.md)、[ETF V3.2 代表性](etf-v3.2.md)、
[OCTOP 原生自动化](octop-native-automation.md)、[QQ 模板](qq-message-templates.md)。

## 0. 一张图

```
OCTOP 定时任务（09:35 / 10:35 / 13:05 / 14:05 / 14:30）
   │ prepare：程序冻结当轮输入（板块行情、成分股报价、ETF 关联池、数字事实目录）
   ▼
AI 按 skill 逐个板块判断 ──► 每个板块一项：phase、evidence_refs、fact_refs、etf_choices
   │ submit                      （可选：core_leader_codes、core_stock_codes、research）
   ▼
程序核验与规范化 ──► 状态 UP / HOLD / WEAK / EXIT / UNKNOWN
   │                 引用不合约、数字不合约的板块单独降为 UNKNOWN
   ▼
轮动链 ──► 是否在活动方向里、连续次数（加强×2、减弱×5），重复证据不计数
   ▼
ETF 穿透评价 ──► AI 选的 ETF 是否来自本板块关联池、代表性是否允许入场、产品和报价能否执行
   ▼
候选 ETF（candidates）──► 板块状态为 UP 且该 ETF 没有活动周期 ──► 登记买入（WAIT_BUY）
   ▼
之后的报价成交 ──► 冻结退出计划（大盘三档 × ETF 的 ATR20）──► 持仓（OPEN）
   ├─ 每条报价：止损 → 保本 → 移动止盈 → 到期 → 目标止盈减半
   └─ 每份报告：退潮全清；减弱首次减半，跨交易日第 2 次全清
   ▼
卖单等待报价（T+1 可卖）──► 轮后 30 / 120 / 300 秒补价，至多 3 次 ──► 减仓 / 全清
```

## 1. 每轮给 AI 的输入

程序在 prepare 阶段冻结一份上下文，AI 只能看到这份数据，不能自己取行情或调交易工具。

**哪些板块进入本轮。** 全部已采集板块按涨幅和相对强度做有界发现（`market-discovery-v2`，新方向上限 8 个），
再并上当前活动方向和持仓关联板块。以 11/20 14:05 那轮为例：已采集 243 个板块，送 AI 判断 23 个
（其中 19 个是已有的活动方向），其余 220 个本轮没有评估。

**每个板块的字段。**

| 字段 | 含义 |
|---|---|
| `sector_id`、`sector_name` | 开盘啦板块稳定 ID 和中文名 |
| `prior_active` | 该板块此刻是否已在活动方向里 |
| `current` | 本轮板块观测：涨跌幅、相对强度、观测时间、引用号 `ref` |
| `previous` | 上一份已发布报告里的同板块观测；跨日的只作背景 |
| `facts` | 程序算好的事实：板块涨跌、较上一轮的变化、成分股涨跌家数（breadth）、质量标注 |
| `stock_evidence` | 本板块成分股的报价、成交额、成员日期，供选核心领涨股 |
| `etf_options.products` | 本板块可选的 ETF：跟踪指数、暴露、代表性、产品规则、报价、规模和日均成交额（参考） |
| `etf_options.observation_products` 等 | 只作观察的关联 ETF、被省略的数量、资料缺口 |
| `fact_catalog` | 可引用的数字事实目录，每条带口径（单板块 / 成分股 / 全部已采集板块 / 已采集 A 股） |
| `held` | 该板块是否关联当前持仓 |

代码入口：`stage2/octop_analyst.py`（`prepare`）、`stage2/native_rounds.py`、`stage2/research_facts.py`、
`stage2/penetration.py`（`prepare_etf_facts`、`freeze_pair_facts`）。

## 2. AI 的输出契约

AI 对每个板块提交恰好一项。数值、中文摘要和 QQ 正文都由程序按引用生成，AI 不写数字。

| 字段 | 必填 | 约束 |
|---|---|---|
| `sector_id` | 是 | 必须是本轮输入里的板块 |
| `phase` | 是 | `NEW` 新增、`MAINTAIN` 维持、`STRENGTHEN` 加强、`WEAKEN` 减弱、`RETREAT` 明确退潮、`UNKNOWN` 待核实、`NOT_COVERED` 本轮未覆盖、`EVENT_ONLY` 事件先行 |
| `evidence_refs` | 是 | 只能引用本板块 `current.ref`；有 `current` 时至少一个 |
| `fact_refs` | 是 | 从本板块 `fact_catalog` 选至多 4 项，有板块数字事实时至少 1 项 |
| `etf_choices` | 是 | 至多 3 个，只能来自本板块 `etf_options.products`，可以为空 |
| `core_leader_codes` | 否 | 至多 8 只，核心领涨 / 中军，只能来自 `stock_evidence` |
| `core_stock_codes` | 否 | 至多 4 只，个股观察名单 |
| `research` | 否 | 仅限指定的深度解释板块：角色、定性理由（90 字内）、反证状态和引用、失效条件、关键未知 |

`phase` 的前提由程序校验：

* `NEW` 要求 `prior_active=false`，不需要 `previous`。
* `MAINTAIN` / `STRENGTHEN` / `WEAKEN` / `RETREAT` 要求 `prior_active=true` 且有可比的 `previous`。
* 不满足前提、引用了不存在的证据、或缺少该板块的判断：只把该板块降为 `UNKNOWN`（没有 `current` 时为 `NOT_COVERED`），
  报告里记 `OCTOP_DECISION_UNCONFIRMED`，其他板块照常发布。
* `fact_refs` 不合约、理由里手抄了数字：同样只把该板块降为待复核，原始判断保留备查。
* `etf_choices` 不合约：该板块标记 `ETF_SELECTION_UNCONFIRMED`，不产生候选。

代码入口：`stage2/native_rounds.py`（`_PROMPT_INSTRUCTIONS`）、`stage2/octop_decision.py`（`render`）、
`stage2/research_facts.py`（`judgment_review`、`selection_detail`）、`skills/a-share-hotspot-scout/SKILL.md`。

## 3. 从 phase 到交易状态

规范化（`adapter.py`）把每个板块变成一个状态，买卖只看这个状态，不读正文。

| AI 的 phase | 报告里的写法 | 交易状态 | 对买卖的含义 |
|---|---|---|---|
| `NEW` | 新增 / `STRENGTHENING` | `UP` | 可以新开仓 |
| `STRENGTHEN` | 加强 / `STRENGTHENING` | `UP` | 可以新开仓；持仓的减弱天数清零 |
| `MAINTAIN` | 维持 / `STABLE` | `HOLD` | 不开仓；持仓的减弱天数清零 |
| `WEAKEN` | 减弱 / `WEAKENING` | `WEAK` | 不开仓；持仓按第 6 节减仓或清仓 |
| `RETREAT` | 撤回 + 外围 `judgments` | `EXIT` | 持仓全清 |
| `UNKNOWN` / `NOT_COVERED` / `EVENT_ONLY` | 待核实 / 本轮未覆盖 / 事件先行 | `UNKNOWN` | 不开仓，持仓不动 |

以下情况一律落到 `UNKNOWN`：报告日期不是当天、报告状态 `UNAVAILABLE`、板块没有来源引用、证据状态为未核验、
`observation_state` 与 `signal` 对不上、只写「撤回」却没有带依据的 `RETREAT` 判断。

### 轮动链和连续次数

`rotation_chain.py` 给每个板块维护一条链，决定它算不算活动方向和连续次数。

| 情况 | 结果 |
|---|---|
| 不在链里，本轮 `NEW` | 入链，成为活动方向 |
| 不在链里，本轮不是 `NEW` | 降为 `UNKNOWN`（`NO_ACTIVE_DIRECTION`），不入链 |
| 已在链里，本轮又 `NEW` | 按「维持」处理（`ALREADY_IN_CHAIN`），状态 `HOLD`，不计数 |
| 已在链里，`MAINTAIN` / `STRENGTHEN` / `WEAKEN` | 与上次相同则连续次数 +1，否则从 1 重新计 |
| `RETREAT` | 出链；之后要重新 `NEW` 才再入链 |
| 本轮证据键与之前某轮相同 | 沿用上次的状态和次数，交易状态记为 `HOLD`，标 `duplicate_evidence`，不计数 |
| 同一时刻多份报告 | 取最保守的一份（退潮 > 减弱 > 维持 > 新增 > 加强），其余记 `COINCIDENT_REPORT_SUPERSEDED` |

连续次数只用于展示（加强×2、减弱×5）和工作台分组，**不参与买卖**：买入只看 `UP`，
卖出按交易日数「减弱」的天数（第 6 节），不看 ×N。

代码入口：`adapter.py`（`normalize`）、`rotation_chain.py`（`advance`）、`ledger.py`（`_advance_rotation`）。

## 4. ETF 穿透：从板块到可买的 ETF

**映射数据。** `maintained_mappings` 每个版本保存板块成分股、ETF 目录、指数权重和产业归属。
一只 ETF 能穿透到某个板块，前提是它跟踪的指数有权重数据，且权重成分与板块成分股有交集，或者有已核验的显式板块—指数关系。
11/20 的数据：目录 1469 只 ETF，其中 172 只能穿透到 36 个板块；另有 1297 只（373 个指数）没有指数权重，进不了穿透。

**评价口径。** 新开仓只有一套评价 `etf-direction-first-v2`，QQ、工作台和入场读同一份保存结果。
主板占比、暴露、核心股集中度、涨幅、均线、规模、日均成交额都只是参考（`REFERENCE`），不否决方向。

一只 ETF 要成为候选，以下五条都要满足：

1. **AI 明确选了它。** 在该板块的 `etf_choices` 里，且选择本身合约。AI 不选，就不会有候选。
2. **板块状态是 `UP`**（新增或加强）。
3. **代表性允许入场**（执行模式 `V3_ENFORCE`）。暴露过弱、单只股票串味、无重合会被拒绝；
   成员数不少于 300 的板块，还要求有已发布的产业归属或显式关联，否则是 `BROAD_SECTOR_UNATTRIBUTED`，只作观察。
4. **能执行。** 产品身份和交易规则已核验、在境内被动行业 / 主题 ETF 范围内、已上市、有交易单位、
   有当天可用报价且未停牌。缺资料是 `PENDING`，不满足规则是 `BLOCKED`，都不产生候选。
5. **同一指数只留一只。** 同板块内按 AI 的顺序去重；跨板块时同一指数取 AI 排位靠前、日均成交额更大的一只。

页面和 QQ 上的四种显示对应的就是这几条：

| 显示 | 条件 |
|---|---|
| 本轮候选 | 五条全过（`CONFIRMED`） |
| 穿透观察 | 有关联，但 AI 没选，或板块不是 `UP`，或被同指数的另一只替代（`OBSERVE`） |
| 执行限制 | AI 选了、板块 `UP`，但产品规则或交易状态不允许（`REJECTED`） |
| 资料待补 | AI 选了、板块 `UP`，但缺核验资料或可用报价（`PENDING`） |

代码入口：`stage2/penetration.py`（`evaluate_recommendations`、`_execution_facts`、`recommendation_candidates`）、
`stage2/etf_representativeness.py`、`stage2/mapping_maintenance.py`、`stage2/worker.py`（`publish_round`）。

## 5. 买入

报告发布时，账本对每只候选 ETF（`ledger.py` 的 `_apply`）：

* 该 ETF 已有活动周期（等待买入、持仓、等待卖出）就跳过。同一运行空间同一 ETF 最多一个活动周期。
* 只取板块状态为 `UP` 的关联；同一批报告里该板块另有「减弱」或「退潮」则不买。
* 一只 ETF 被多个板块选中时，先取暴露档位为核心代表的，再按 AI 排位。这个板块成为周期的**主板块**，以后的卖出只看它。
* 新建周期，状态 `WAIT_BUY`，带一笔买单。每个周期有自己的资金账本（规则默认初始资金 10000 元），按成交报价能买的整手数买入。

成交：

* 买单由**信号之后**的报价成交，不用信号当时的价格；报价要可交易、在交易时段。
* 成交前再查一次入场准入：资料待复核（`review_required`）或准入读不到时暂停买入，不影响已有持仓的卖出。
* 等待买入期间主板块转为「减弱」或「退潮」，买单取消，周期关闭。
* 买入成交的那条报价不判退出规则，只冻结退出计划。

代码入口：`ledger.py`、`signal_cycle.py`（`create_cycle`、`fill`）、`stage2/entry_admission.py`。

## 6. 卖出（hermes_signal_cycle_v2）

买入成交时按**成交日前一交易日**的大盘档位和这只 ETF 的 ATR20 算出各条线，写进周期后不再变。
数值表和档位判定见 [退出规则 v2](hermes_signal_cycle_v2.md)，这里只列顺序。

**每条报价**（五轮的报价和轮后补价），先撮合已有订单，再按顺序判，命中第一条就停：

| 顺序 | 条件 | 动作 |
|---|---|---|
| 1 止损 | 浮盈 ≤ −止损线 | 全清 |
| 2 保本 | 已有减仓成交、保本已武装、价格 ≤ 成本 × 1.003 | 全清 |
| 3 移动止盈 | 峰值浮盈 ≥ 启动线，且自峰值回撤 ≥ 回撤线 | 全清 |
| 4 到期 | 14:05 之后：持有交易日 ≥ 上限；或 ≥ 滞涨天数且浮盈 < 滞涨线 | 全清 |
| 5 目标止盈 | 没减过仓、没有待成交订单、浮盈 ≥ 目标线 | 减半，并武装保本 |

峰值浮盈达到 +1.5% 或目标止盈减半后，保本武装，周期内不回退。

**每份报告**（只看周期的主板块）：

| 主板块状态 | 动作 |
|---|---|
| `EXIT`（退潮） | 全清 |
| `WEAK`（减弱） | 按交易日计数：首次减半；跨交易日的第 2 个减弱日全清。同一天多次减弱只算一天 |
| `HOLD` / `UP` | 减弱天数清零（重复证据的沿用不清零） |

`hermes_signal_cycle_v2_0_1`（`MODEL_EVALUATION_RULE=v2.0.1`，已有代码，执行服务器发布后才生效）只改上表最后一行：
`HOLD` 不再清零，只有非重复证据的 `UP` 清零。v2 周期仍按上表。
| `UNKNOWN` 或主板块没出现在报告里 | 不动 |

**成交。**

* 卖单遵守 T+1：当天买的份额次一交易日 09:30 之后才可卖。
* 卖单由之后的报价成交。每轮发布后 30 / 120 / 300 秒各补一次价，至多 3 次。
  14:30 轮有当日可卖的待卖订单时，这 3 次改为发布后 1 分钟、5 分钟和 14:50（同样在执行服务器发布后生效）；
  只有买单在等、或其他轮次时不变。
* 当天最新一轮失败时，用该轮自己的 3 次补价给已触发的卖单成交，买单不动。
  研判阶段挂掉的轮次要等 10 分钟期限到了才记为失败，补价从那时开始。
* 已登记全清（`WAIT_EXIT`）之后，不再判规则，报告也不再改订单。

代码入口：`signal_cycle.py`（`process_quote`、`evaluate_exit`、`_apply_report_exits`）、
`simulator.py`（`EXIT_TABLE`）、`stage2/market_regime.py`、`stage2/etf_statistics.py`、`stage2/pending_quotes.py`。

## 7. 审核 AI 报告时值得对照的地方

这些是代码里的事实和上周（11/16–11/20，21 份已发布报告、4 轮失败）执行服务器的只读统计，不是结论。

1. **入场很窄。** 买入要同时满足「AI 判新增或加强」和「AI 在同一轮选了 ETF」。上周 `UP` 的板块轮次 31 个
   （新增 19、加强 12），其中 AI 同时给了 `etf_choices` 的只有 4 个；另有 30 个带选择的板块轮次不是 `UP`，只成了观察项。
   全周产生候选 ETF 7 只次（已有活动周期的 ETF 不重复开仓）；账本里现有 3 个周期。
2. **「新增」之后 AI 才选 ETF 就晚了。** 板块第二轮起只会是维持 / 加强 / 减弱，其中只有「加强」还能开仓。
   skill 里写的是「`NEW` 可以只有观察方向」，没有要求新增当轮给出 ETF。
3. **连续次数不进买卖。** 报告和页面显示的 减弱×5、加强×2 只是展示；卖出按「减弱的交易日数」，买入不看次数。
4. **「待核实」占比高。** 上周全部板块判断里 `UNKNOWN` 91 次，仅次于减弱 139 次，高于维持 84 次。
   `UNKNOWN` 对持仓等于不动：既不减仓，也不清零减弱天数。
5. **卖出只看主板块。** 周期建立时定下一个主板块；ETF 同时关联的其他板块转弱不会触发卖出。
   主板块某轮没被送去判断（不在发现结果里也不在活动方向里）时，这一轮对持仓没有任何报告信号。
6. **「退潮」极少。** 上周只有 1 次。报告层面的清仓主要靠「跨交易日第 2 个减弱日」，价格层面靠止损和移动止盈。
7. **重复证据不算新判断。** 两轮之间板块观测没更新时，AI 写什么都按沿用处理，交易状态记 `HOLD`，
   既不新增减弱天数，也不清零。
8. **每轮只评估一部分板块。** 11/20 14:05 那轮 243 个板块里评估了 23 个，新方向上限 8 个；
   没进发现结果的板块当轮不可能「新增」。
9. **穿透覆盖有缺口。** 1469 只 ETF 里 1297 只缺指数权重，AI 的可选池只来自有权重或有显式关系的那一部分；
   现有权重日期是 2026-10-30。
10. **QQ 简报是程序排版的。** 五轮报告用的模板是 `deploy/qq-templates/research.txt`（`qq-template-v2`，
    源文件 `modules/strategy_hermes/qq_templates/research.txt`）；skill 目录里的 `qq-brief.md` 是旧格式
    `octop-markdown-v1` 的模板，11/16 之后的报告不再使用。数字由程序按 `fact_refs` 渲染；
    AI 能影响的只有方向、引用了哪些事实、选了哪些 ETF 和定性理由。

## 8. 执行服务器当前状态（2026-11-21）

| 项 | 值 |
|---|---|
| 版本 | `ETFv2.0`，`main` 提交 `8eb0720` |
| 退出规则 | `MODEL_EVALUATION_RULE=hermes_signal_cycle_v2`，`REGIME_MODE=DUAL` |
| 持仓 | 3 个周期，状态 `OPEN`，均为 v2、带退出计划（由 v1 迁入，`CLAUDE.md` 第 32 条） |
| 研判触发 | OCTOP 原生自动化，专家 `my-stock`，skill 在 `/opt/1panel/apps/octop/octop/data/.octop/agents/my-stock/.octop/skills/a-share-hotspot-scout/` |
| ETF 代表性模式 | 11/20 的报告按 `V3_ENFORCE`、`etf-representativeness-v2` 发布 |

自动化开关（五轮研判、采集、QQ、个股研究、IOPV、触发模式、代表性模式）由发布时的 `--automation` 设置文件决定，
不在镜像里；见 [发布操作手册](../deploy/README.md)。
