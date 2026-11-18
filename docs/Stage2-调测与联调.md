# Stage 2 调测交付

调测机 106.53.186.201，项目目录 /opt/workspace/stock-v4。当前验收使用冻结业务样本和真实调测 PostgreSQL、Redis。真实 Hermes、行情、MX 和 QQ 联调待输入和投递入口确认。

## 代码入口

- modules.strategy_hermes.stage2 提供 migrate、tick、publish-report、apply-quotes、inspect、report 和 flush-redis。
- stage2/postgres.py 保存原报告和规范化结果，同一事务提交轮次、候选、轮动、订单、成交和 Outbox。轮次和活动 ETF 周期有数据库唯一约束。报价也在同一事务更新持仓和缓存投递队列。
- stage2/inputs.py 读取 Redis 或本地文件的旧共享快照契约，按观测时间和修订选择当前快照。Redis 缺键或不可用时从 PostgreSQL 恢复已保存快照。
- stage2/penetration.py 复用旧版指数权重归一化、板块暴露、核心权重和代表性 ETF 排序。覆盖、IOPV、可选规模和权重年龄作为质量说明。产品分类、完整可核验权重、真实板块成员和本版静态门槛决定哪些 ETF 可以成为候选。旧版盘中 VWAP、MA5、加强次数和时效条件在本入口不参与买卖决策。
- stage2/worker.py 提供五轮时间窗口、任务租约、独立请求预算和报告收件目录。tick 单次运行，消费外部 Hermes 已发布文件。当前轮次恢复窗口默认 8 分钟。run_due 可供真实 Hermes 调用适配器使用，实际触发配置和部署待联调。
- stage2/outbox.py 独立认领、投递和重试。已验证 Redis 的真实写入。QQ 模拟失败复用同一事件键，报告与交易节点不会重建。

## 在调测机复现

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"

"$python" -B -m modules.strategy_hermes.stage2 --env-file "$runtime/.env.test" --input-kind FROZEN migrate

STAGE1_TEST_ROOT="$runtime/test-tmp/stage1-tests" \
STAGE2_TEST_ENV_FILE="$runtime/.env.test" \
"$python" -B -m unittest discover -s tests -v
```

调测运行环境依赖在 requirements-stage2.lock 锁定到 CPython 3.12/Linux x86_64 的已校验 wheel。当前只在调测测试库和开发库建立 stock_v4_stage2 schema。移除阶段内测试数据时按随机测试 namespace 定位，测试账号不会访问开发库。

## 真实接入所需输入

- 快照输入支持 market_read_v1 或 hotspot_market_snapshot_v1。可指定调测 Redis 环境的键或本地只读文件。保留 trade_date、observed_at、来源、quote 时间和修订。同日迟到的报价按原始观测时间处理，已知停牌、非连续交易时段和跨日历史报价不参与实时参考成交。
- 日历读取 hotspot_trade_calendar_v1，需覆盖当前日期及下一交易日。映射读取 stock-v4-etf-mapping-v1，包含版本、sectors 的成员、products 的跟踪指数和交易单位，以及 weights 的权重日期、完整性和权重行。该映射封装是本阶段选择的本地接入格式，收到真实源后需要校对字段、单位和供应方更新方式。
- Hermes 报告输入保留旧版原 JSON 字符串及实际发布时间。外围包含 input_kind、published_at、report_json 和可选 judgments、analyst_version。退潮按 skills/a-share-hotspot-scout/SKILL.md 的结构化标记。文件交接支持 inbox/YYYY-MM-DD/HHMM.json。真实 Hermes 启动入口和受控 MX 调用仍待确认。
- OCTOP 容器健康。现有应用配置中的推送地址、目标仍是占位项。真实 QQ 联调需要确定正式可调用的投递方式和明确的测试目标。

## 当前验收结果

33 项通过。真实 PostgreSQL 和 Redis 验证了 60% 权重报价覆盖、缺 IOPV、较旧权重下的穿透和候选排序。入场 1,000 份，当日减仓等 T+1，随后减仓 500、退潮清余 500，重连账本仍有 3 笔成交和 1,500 元冻结毛利润。Redis 故障回退读取 PostgreSQL，晚到旧快照不覆盖新快照。模拟 OCTOP 第一次超时后只重试投递，旧轮次租约不能迟到发布。CLI 验证一轮报告损坏时仍可以推进已有订单。

实时供应商和采集任务、Hermes 端的工具权限与五轮真调度、真实 MX/OCTOP 投递、三日历史触达以及生产发布验收仍待后续实施或联调。

## 重构环境的首批接口实测

采集入口为 `modules.strategy_hermes.stage2.tushare_bootstrap`。使用旧备份的
`TUSHARE_CONFIG_FILE` JSON 结构，字段为 token、可选 base_url 和 api_overrides。
调测测试库迁移 0002 已应用，建立采集批次、交易日历、ETF 基础资料和指数权重四张表。
凭据文件就绪后，先单次验证 trade_cal、etf_basic、index_weight，再依据实际返回与权限决定后续数据源。
指数权重按百分数转比例，完整性需要另行验证。

OCTOP 即本项目的 Hermes。执行机应用健康，已有一个启用的 QQ 通道。
容器包含受鉴权的专家接口和本地 ACP 入口，真实轮次需联调专家的技能加载、工具范围和报告回传。

### 收盘接口与行情来源

按已确认的分工，Tushare 负责 LEFT/RIGHT 盘后数据，开盘啦提供大盘和板块资料，
TickFlow 提供 ETF 轮动行情，Findesk 提供可选 IOPV。
测试库迁移 0003 增加收盘日线表。官方地址初次探测返回 40101。
使用用户确认的 Lianghua 网关和兼容 HTTP POST 复测成功，2026-11-09 至 2026-11-11
写入 3 条日历和 000001.SZ 的 3 条日线。相同请求重试返回 ALREADY_STORED。
收盘测试适配沿用原单位换算，成交量手转股、成交额千元转元、涨跌百分数转比例。

### 真实源首批入库结果

调测库 stock_v4_test 已应用迁移 0004。接口入口为
`python -B -m modules.strategy_hermes.stage2.market_bootstrap`。

- TickFlow 使用小样本 symbols 请求。510300.SH、512480.SH 各保存一条 2026-11-12 报价，
  源时间分别为 15:00:02 和 15:00:26，接收时间另存。
- 开盘啦从 /opt/workspace/legacy_backup/kaipanlajson 加载获准捕获样例。
  016、018 均有实测返回。041 首屏 60 条，供应商报告 243 条。
  046 验证了档案样例 70 条和 801001 于 2026-11-11 的两页成分。
  两页 1,000 与 145 条无交集，合计 1,145 个唯一原始代码，等于供应商报告总数。
  801001 的 2026-11-12 查询返回 1020，该次未写入成分记录。
- Tushare 的 etf_basic 保存 512480.SH 基础资料，跟踪指数 H30184.CSI。
  index_weight 保存 2026-10-30 的 87 行，独立成分清单尚待核验。
- 33 项现有测试通过。实测只写入调测测试库。行情和成分样本仍待组装成持续更新的共享快照，
  并用于 OCTOP/Hermes 的真实轮次联调。

### 批量采集与真实快照

在调测库 stock_v4_test 应用 0005、0006 后，批次 stock-v4:test:bulk-20261112-v1 实测如下。

| 数据 | 请求数 | 实际入库或组装行数 |
| --- | ---: | ---: |
| TickFlow CN_ETF | 1 | 1,634 |
| Tushare 交易日历 | 1 | 33 |
| Tushare ETF 基础资料 | 1 | 1,438 可识别，302 行保留原始数据供后续校验 |
| Tushare 证券主数据 | 1 | 5,580 |
| 开盘啦 041 | 5 | 243 个唯一板块，等于供应方总数 |

以同一个批次键重跑全部入口，新增供应商请求为 0。开盘啦 046 复用已完整入库的 2026-11-11 芯片板块两页，1,145 个原始代码全部通过证券主数据转换。H30184 的 87 个 Tushare 权重成分和中证指数同一观察窗口披露的 87 个样本逐代码匹配，保留原表、日期、来源及散列。权重为 2026-10-30，独立清单为 2026-11-11。

调测快照保存在 PostgreSQL，Redis 键为 stock-v4:test:source:market。持久文件位于 /opt/workspace/stock-v4-runtime/stage2-derived。
试验映射覆盖 512480.SH 与开盘啦芯片板块，指数交集 86 只。试验产品的主板权重约 26.4%，低于既有 65% 筛选线，且产品涨跌幅规则尚待逐产品核验。观察解释和独立 OCTOP 预览输入已生成，用于下述盘后单次模型测试。

入口为 modules.strategy_hermes.stage2.bulk_collect、modules.strategy_hermes.stage2.build_sources 和 modules.strategy_hermes.stage2.octop_preview。当前共 34 项测试通过，覆盖批量请求复用和重启后的逻辑预算。

### OCTOP 独立只读专家试跑

执行机 OCTOP 1.0.2 已创建私有专家 `stock-v4-stage2-readonly`，状态为停止。OCTOP 技能目录已识别启用的 `a-share-hotspot-scout`，文件与交付包的 SHA-256 一致。专家使用专用文件工作区，未绑定 QQ、定时任务、MCP 或知识库。当前唯一启用的模型为 `deepseek-v4.1-flash`。

2026-11-12 20:27（北京时间）使用原 OCTOP 镜像和模型工厂，在隔离只读容器中把已安装技能和已保存快照交给模型。HTTP 层实测一条 POST，返回 200，未提供工具。模型回报标识为 `deepseek-v4.1-flash-eco`。盘后快照保留开盘啦板块观测时间 19:58:49、TickFlow ETF 报价时间 15:00:26 和 11 月 11 日的板块成员日期。

原回复的摘要把芯片板块标为「待核实」。输出 token 用满 4096，其中推理占 3521。回复截断在第 27 行，JSON 解析失败，本次结构化验收未通过。保留原始回包和失败记录，未提交正式报告、信号或交易节点。后续模型验收应先让必填字段进入短响应，并为推理与正文预留输出空间。

原始结果归档到调测机 `/opt/workspace/stock-v4-runtime/stage2-derived/octop-readonly-test-632d3874eafbe6537af20d04.json`。模型输入和响应经标准流传递，归档使用 `APP_TMP_ROOT` 下的统一临时任务目录，落盘后已回收本次暂存目录。执行机 OCTOP 健康，`my-stock` 仍在运行，其 QQ 通道处于启用状态。

### OCTOP 盘后复测与报告接收

第二次只读试跑使用同一盘后快照、同一已装技能及当前模型。提示词压缩为一条板块观察，输出上限调为 12288 token。实测一次 POST，HTTP 200，返回完整 JSON，输出 5825 token，其中推理 5158 token。原始报告通过现有 `hermes_sector_rotation_v1` JSON Schema 校验，接收器复核快照和技能散列、板块 ID、行情来源时间和 ETF 排除原因。芯片状态为 `PARTIAL / 待核实 / UNCERTAIN`。

模型正文把成分股报价覆盖偏低写成了 ETF 穿透覆盖偏低。接收器保留原文，并按快照单独归档程序计算的指标。芯片板块暴露约 99.82%，成分股报价覆盖为 0%，主板权重约 26.4%。正式报告链路仍需验收文字与结构化事实的一致性。盘后试跑使用 `READ_ONLY_OBSERVATION`，正式报告规范化和落库入口会拒绝这种回包。

报告接收入口为 `modules.strategy_hermes.stage2.octop_receive`，原始回包、规范化观察和快照指标归档于 `/opt/workspace/stock-v4-runtime/stage2-derived/octop-readonly-test-393e62e37f49ec4164a97276.json`。以下命令重放已保存的回包，返回同一份归档。

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"
artifact="$runtime/stage2-derived/octop-readonly-test-393e62e37f49ec4164a97276.json"
"$python" -B -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["original_response"]))' "$artifact" |
  APP_TMP_ROOT="$runtime/test-tmp" "$python" -B -m modules.strategy_hermes.stage2.octop_receive \
    --preview "$runtime/stage2-derived/octop-preview-3cce2d58ca06c0c27357284b.json" \
    --skill skills/a-share-hotspot-scout/SKILL.md \
    --output-dir "$runtime/stage2-derived"
```

调测 PostgreSQL 与 Redis 已通过 36 项回归测试。新增五轮时点的冻结回放检查，五轮各认领一次，首轮开仓后其余四轮均携带持仓关联板块，报告缺席时仍保留持仓。另验证把盘后预览提交给正式入口时，报告、订单和投递事件均为 0。真实盘中触发与回传、受控 MX 和项目侧 QQ 投递仍待联调。

### OCTOP 方向与行情事实一致性验收

调测接收器使用 `modules.strategy_hermes.stage2.octop_decision`。模型只返回板块标识、基础状态和当前证据引用。应用从快照生成报告正文、收益率、权重暴露和报价覆盖，并保留各源时间。接收器拒绝重复 JSON 字段、额外的模型事实字段和无效证据引用。缺本轮证据时保留最近有效来源时间。报告仍使用 `hermes_sector_rotation_v1`，用途标记为 `READ_ONLY_OBSERVATION`。

当前 OCTOP 模型完成七次隔离只读调用，每个用例一次 POST，全部 HTTP 200。七份应用生成的报告均通过 Hermes 契约校验。样本包括一条真实盘后方向冲突和六条冻结用例，覆盖新增、维持、低覆盖加强、减弱、明确退潮、持仓板块本轮未覆盖。真实快照的板块权重暴露为 99.82%，成分股报价覆盖为 0.00%，开盘啦板块来源时间为 19:58:49，TickFlow ETF 报价时间为 15:00:26。真实样本状态为「待核实」。

调测数据库的冻结回放复用了六条已归档的模型结果。五轮各认领一次，新增买入 1,000 份，同日转弱的 500 份目标等待 T+1，次交易日减仓 500 份。明确退潮移出轮动链并清余 500 份。重开账本核对三笔成交和 1,500 元冻结样本毛利润。全部 40 项回归测试通过。

归档 `/opt/workspace/stock-v4-runtime/stage2-derived/octop-direction-validation-8e3623bce23e1643f1117210.json` 保存输入、原始模型回包、程序生成报告和逐例判定。以下命令重放已归档回包并运行调测回归，重放无需模型调用。

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"
artifact="/opt/workspace/stock-v4-runtime/stage2-derived/octop-direction-validation-8e3623bce23e1643f1117210.json"
"$python" -B -c 'import json,sys; a=json.load(open(sys.argv[1])); print(json.dumps({"protocol":a["protocol"],"receipts":[r["original_response"] for r in a["results"]]}))' "$artifact" |
  APP_TMP_ROOT="$runtime/test-tmp" "$python" -B -m modules.strategy_hermes.stage2.octop_validate \
    --preview "$runtime/stage2-derived/octop-preview-3cce2d58ca06c0c27357284b.json" \
    --skill skills/a-share-hotspot-scout/SKILL.md --output-dir "$runtime/stage2-derived"

STAGE1_TEST_ROOT="$runtime/test-tmp/stage1-tests" \
STAGE2_TEST_ENV_FILE="$runtime/.env.test" \
OCTOP_DECISION_ACCEPTANCE_FILE="$artifact" \
"$python" -B -m unittest discover -s tests -v
```

真实盘中触发与报告回传、MX 证据在五轮中的接入和项目侧 QQ 实际投递待联调。执行机模型试跑仍通过独立只读专家进行。

### OCTOP 内置 MX 只读联调

MX 由执行机 OCTOP 侧调用。复用现有 `my-stock` 专家配置中的 `MX_APIKEY` 和已安装的 `mx-search` 技能。调测机接收脱敏结果。独立只读模型专家通过受控后台接收资讯证据，密钥留在 OCTOP。

2026-11-12 21:02（北京时间）在 OCTOP 容器中直接调用已安装技能的 `MXSearch.search()`。测试查询覆盖芯片行业公开新闻和公告，物理请求一次，返回 HTTP 200。收到 12 条，按标题和发布时间去重得到 10 条。归档保留 5 条元数据样本、供应方原始发布时间、检索观测时间及原响应散列。资讯证据与行情快照分别保留来源。

本次试跑验证了配置、只读检索和脱敏回传。正式五轮仍需把检索接入 worker 的 `RequestBudget.call("mx", ...)`，用同一轮次任务记账并隔离测试逻辑额度。归档路径为 `/opt/workspace/stock-v4-runtime/stage2-derived/octop-mx-probe-acbae806545c6ff48789fa98.json`。

### Stage 2 worker 五轮调度与 OCTOP 接入

运行入口为 `python -B -m modules.strategy_hermes.stage2 ... serve`。worker 使用交易日历与北京时间，以 15 秒间隔检查 09:35、10:35、13:05、14:05、14:30。每轮保留 8 分钟启动窗口。PostgreSQL 按命名空间、交易日、轮次唯一认领，并存储实际请求次数、报告、轮次与投递事件。已有持仓的主板块和关联板块每轮独立跟踪。

`octop_analyst` 优先为已有持仓补充证据。10:35 有可用板块行情时可查询一次，其他轮次在持仓板块出现相对可比前序的方向反转时查询。MX 与研判分别记账，单轮 MX 上限一次，研判最多两次，明确拒绝会结束该轮模型调用。MX 超时、配额用尽和截点前无结果均写入报告，worker 仍可使用已有行情。MX 证据按原始发布时间过滤，缺时区的供应方本地时间按北京时间处理。资讯引用不能独立确认轮动强弱。

`octop_bridge` 在 OCTOP 容器读取已安装的只读技能和配置，提供 `mx` 与 `analyze` 两项标准流操作。凭据留在 OCTOP。worker 通过 `--octop-command-json` 指定受信的桥接命令，进程退出后可重新建立。调测使用 `--octop-stdio` 和操作机 SSH 中继，执行机无需新增端口或复制密钥。

调测时钟设置为 2026-11-12 10:35，行情为明确标注的冻结样本。最终验收命名空间 `stock-v4:test:octop-schedule-20261112-final` 中，真实 MX 与 OCTOP 模型各调用一次，均为 HTTP 200。MX 有 5 条记录通过截点过滤。报告 `octop-9d527c40e0540bd080c390a21f8e` 在 PostgreSQL 保存为 `PARTIAL`，芯片判为待核实。实际生成时间 21:26:31，计划时间和数据观测时间分别保留。Redis 投递成功，产生 5 个共享视图。重复触发返回 `ROUND_ALREADY_CLAIMED`，外部调用次数保持不变。真实测试请求计入 OCTOP 所用 MX 账户，轮次预算记在调测库。

从可访问两台服务器的操作机运行 `tools/stage2_octop_relay.py`。下面用已验收的命名空间复跑去重；用新的测试命名空间可再次发起真实调用。

```bash
runtime=/opt/workspace/stock-v4-runtime
derived="$runtime/stage2-derived"
python -B tools/stage2_octop_relay.py --dev-host tencent --octop-host telestock \
  --dev-known-hosts work/tencent-known-hosts \
  --namespace stock-v4:test:octop-schedule-20261112-final \
  --at 2026-11-12T10:35:00+08:00 \
  --calendar "$derived/calendar-729bbab2fc348decbf6af336.json" \
  --mapping "$derived/pilot-3a55c6ef3f3dd53c48e1b1af.json" \
  --snapshot "$derived/frozen-octop-schedule-20261112.json"
```

验收共 42 项回归测试通过，覆盖五轮触发、非交易日、轮次截止、截点过滤、MX 超时降级、模型明确拒绝、子进程重连和重复认领。实际盘中持续运行与项目侧 QQ 投递待联调。

### QQ 直发、通知重试与实际交易日试跑

- QQ 机器人在现有唯一绑定的私聊中完成直发。联调编号 `stage2-qq-20261112-01`。消息提交一次，腾讯 QQ 返回 HTTP 200 和消息编号。脱敏回执保存在 `/opt/workspace/stock-v4-runtime/stage2-derived/qq-direct-test-17aeb527d6a076362713eb7f.json`。
- worker 的 `serve --qq` 会在自动模拟节点落库后入队。OCTOP 桥接端通过 `STOCK_V4_QQ_ROUTE_SHA256` 固定私聊，报告和通知复用同一提交事务，发送结果独立处理。明确限流可按原事件键重试。发送后结果不明或发送租约过期保留失败记录，等待核对。QQ 连接测试用现有机器人，Outbox 的 429、成功回执和不明结果走调测用例。自动推送待按接收配置实测。
- 11 月 13 日实际交易日试跑已在操作机安排 09:31 北京时间启动。调测服务器使用 `stock-v4:dev:stage2-live-20261113`，市场输入用专用 Redis 键，报告和视图也按同一命名空间隔离。OCTOP 桥接通过现有 SSH 登录，操作机需要保持登录、在线。worker 在 14:38 收尾。
- 五轮各尝试一次 TickFlow `CN_ETF` 全池批量查询和最多五页开盘啦板块数据，按轮次持久化预算。行情与来源时间分开保存。已通过双服务器连接预检，盘中实际运行结果待 11 月 13 日核对。试跑使用现有芯片映射和调测 Redis 投递。
- 调测及开发库均已应用迁移 0007。44 项回归测试通过，包括 QQ 明确限流重试、回执缺失隔离，以及盘中部分数据合并。

回放与试跑使用 `tools/stage2_octop_relay.py`。交易日结束后通过下列命令核查已提交报告和通知数：

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
"$runtime/stage2-venv/bin/python" -B -m modules.strategy_hermes.stage2 \
  --env-file "$runtime/.env.development" --input-kind LIVE \
  --dev-namespace stock-v4:dev:stage2-live-20261113 inspect
```

### 2026-11-13 盘中验收清单

- [x] 核对 09:31 北京时间操作机任务启动及 SSH 中继恢复。使用调测命名空间 `stock-v4:dev:stage2-live-20261113`。
- [ ] 核对 09:35、10:35、13:05、14:05、14:30 五轮各自的计划时间、行情观测时间、实际生成与落库时间。逐轮记录状态和失败原因。
- [ ] 核对 TickFlow 全池每轮至多一次，开盘啦最多五页。记录可用行数、跨日或晚到数据、局部缺口。MX 在条件命中时核对当轮预算和资讯截点。
- [ ] 核对报告、模拟节点和 Redis 视图的提交顺序。若出现合格信号和参考价，验证自动模拟单及持仓跟踪。若条件未出现，记录原因并保留待验证分支。
- [x] 使用已绑定 QQ 私聊验证一条项目 Outbox 模拟通知的发送回执及同事件去重。机器人直发测试已通过；实际盘中试跑的现有任务先投递调测 Redis，QQ 联调独立执行。
- [ ] 14:38 后汇总五轮任务、请求预算、报告及持仓结果，记录异常和待复验项。

### 2026-11-13 盘中启动与首轮记录

- 09:31 启动的 Windows 计划任务随后退出，退出码为 `0xc000013a`，调测机留有一个旧 worker。09:35 前通过现有 SSH 中继恢复，并确认旧进程退出。修复后的 worker 发现中继标准输入关闭时会主动退出。盘中采集另使用命名空间级互斥锁。
- 09:35 轮实际于 09:35:26 发布，状态 `PARTIAL`，报告 `octop-b0e5f0268fcce700a296b264c84b`。OCTOP 调用 1 次。Redis Outbox 为 `SENT`，回读 5 个视图。芯片为「新增」。试点产品 512480.SH 的规则核验未完成，主板权重低于现有门槛，因此本轮合格 ETF 候选为 0，模拟订单和持仓均为 0。
- 首轮开盘啦请求 5 页，保存 243 个板块。TickFlow 全池请求 1 次，保存 1,633 条当日有效报价。原报告使用的是 `intraday-281acbb15548a40bfbc9f19c`，当时并发刷新先发布了只含板块的快照。用已落库批次重新合并，保持原始观测时间 09:35:10，发布同时间修订 1，快照 `intraday-3d86aa8faee51625b4cfe006`。原报告、原观测时间和原始供应商批次保留。恢复期间行情请求次数未增加。
- 09:39:36 独立冻结用例通过项目 QQ Outbox 实发。联调编号 `stage2-qq-outbox-20261113-01`，QQ 返回 HTTP 200，Outbox 为 `SENT`，持久回执记录一次消息提交。重复调度返回 `EMPTY`。回执归档于 `/opt/workspace/stock-v4-runtime/stage2-derived/stage2-qq-outbox-20261113-01.json`。
- 09:50 核对后续轮次只剩一个 worker。后续调测继续使用 `stock-v4:dev:stage2-live-20261113`，Windows 中继日志在操作机 `work/stock-v4-stage2-trial/stage2-live-2026-11-13-supervised.err.log`。10:35、13:05、14:05、14:30 的结果及收尾仍需按实际记录验收。61 项回归测试通过，覆盖同观测时间的快照修订、回退及中继断开检测。
- 盘中核对脚本为 `tools.stage2_live_audit`。调测机已设置 10:44 和 14:40（北京时间）两次只读核对，输出到 `/opt/workspace/stock-v4-runtime/stage2-derived/live-trial-audit-2026-11-13-*.json`。09:55 的首份验收记录为 `live-trial-audit-2026-11-13-201c5ac98ed0504c.json`。
- 完成 ETF 未复权历史开盘价和最高价接入及构造信号验算，记录见 `docs/Stage3-手动回测调测.md`。当天真实候选若出现，按交易日历在 11 月 16、17、18 日观察，待 18 日收盘及日线入库后核对实际触达。


### 2026-11-13 上午轮次复核

| 计划时间 | 行情观测时间 | 报告发布时间 | TickFlow 批量 | 开盘啦 041 | OCTOP | MX | Redis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 09:35 | 09:35:10 | 09:35:26 | 1 次，1,633 条 ETF 报价 | 5 页，243 个板块 | 1 次 | 无需调用 | SENT |
| 10:35 | 10:35:21 | 10:35:39 | 1 次，1,634 条 ETF 报价 | 5 页，243 个板块 | 1 次 | 1 次，12 条均在当轮截点前发布 | SENT |

以上时间均为北京时间。两轮任务各认领一次，报告为 `PARTIAL`。10:35 轮报告 `octop-93f48be3567fa203e039d29c57f2` 使用快照 `intraday-5ebcbff47beffb7deddc348e`，芯片由「新增」转为「减弱」。最新一条已采纳的 MX 资讯发布时间为 10:20。调测 Redis 可回读 5 个共享视图。

试跑映射当前包含一只 ETF。512480.SH 主板权重约 26.4%，低于既有 65% 门槛，产品涨跌幅规则待核验。上午模拟订单、模拟成交和实盘记账均为 0。上午报告投递 Redis，QQ 独立联调的 Outbox 已实际发送一次，重复投递返回 `EMPTY`。

10:44 自动验收存档为 `/opt/workspace/stock-v4-runtime/stage2-derived/live-trial-audit-2026-11-13-6e477134c87f6f5f.json`。11:01 复核仍为单 worker，午后三轮为 13:05、14:05、14:30，14:40 自动归档。


### 2026-11-13 生产接管验收

- 调测 13:05 的行情采集成功，研判两次返回不可用，未记到 HTTP 请求。调测任务保留 `FAILED`。生产 13:35 启用，调测中继随后停止。
- 生产 14:05 报告于 14:05:31 发布。银行为「新增」，512800.SH 模拟买入意图已落库。TickFlow 当轮唯一请求失败，开盘啦五页、243 板块成功。QQ 回执 `SENT`，HTTP 200，发送一次。
- 生产 14:30 报告于 14:30:31 发布，银行为「加强」。TickFlow 修复后的当轮唯一请求取得 1,634 条 ETF 报价，开盘啦五页、243 板块。14:05 订单以 14:30:05 的 TickFlow 原始报价 0.899 元模拟买入 11,100 份，下一可卖日为 11 月 16 日。两轮 Redis 均 `SENT`。
- 14:48 尾盘待价预览显示无需要补价的订单，额外行情请求为 0。实盘记账新增记录为 0。
- 生产证据归档 `/opt/workspace/stock-v4-runtime/stage2-derived/production-live-audit-2026-11-13-84b936d6f40bd469.json`。三日触达登记同 ETF 的两份原报告，均等待 11 月 16–18 日的观察窗口。
