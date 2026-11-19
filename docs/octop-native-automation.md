# OCTOP原生板块轮动自动化

2026-11-16 用户确认将五轮研判迁入OCTOP自动化并完成后上线。

## 运行职责

- OCTOP的专用「板块轮动与ETF穿透」专家持有09:35、10:35、13:05、14:05、14:30五个工作日任务，时区Asia/Shanghai。后台交易日历判断节假日。
- 专家通过stock-v4-rounds的prepare/submit/status工具取冻结证据、提交JSON决策、读取已落库反馈。原生会话、自动化状态和usage_log保留执行记录。
- stock worker消费原生任务及经运维授权的发布恢复；负责共享采集、受控MX、结构核验、ETF穿透、T+1模拟、报告/信号落库和Outbox。原来五轮内部时钟关闭。
- 报告、模拟节点和Outbox仍在同一数据库事务保存；QQ投递独立。QQ失败仅重试投递，不重新调用模型。

## 代码入口

| 入口 | 用途 |
|---|---|
| modules/strategy_hermes/stage2/native_rounds.py | 持久队列、10分钟期限、冻结上下文、幂等提交、worker执行与重启对账 |
| modules/strategy_hermes/native_mcp.py | 独立内部8001端口的鉴权MCP，三项受控工具、单请求线程、有界等待 |
| modules/strategy_hermes/runtime.py、stage2/schedule.py | internal/octop触发模式，独立MCP监听和唯一worker |
| deploy/octop_native_guard.py | 专用专家工具白名单、模型预算、真实完成回执核验 |
| deploy/octop_native_delivery.py | 原生任务异常时保留已收到的用量、执行轨迹和失败反馈 |
| tools/prepare_octop_native_patch.py | 验证OCTOP镜像与源码摘要后生成可恢复的兼容覆盖文件 |
| tools/configure_octop_native.py | 通过原生API幂等配置专家、MCP与五个任务 |
| tools/octop_native_local.py | 停机维护期间通过OCTOP官方embedded runtime配置同一套任务 |
| stage2/migrations/0014.sql | 新增native_round_jobs，旧报告/成交表保持原有契约 |

OCTOP 1.0.2会默认附加基础工具，因此专用专家增加工具调用前的强制白名单；其他专家继续原配置。兼容覆盖绑定镜像ID和源文件SHA，升级OCTOP前需重新验证。`max_iters=768`是流程节点预算。研判每轮最多20次模型调用，由独立中间件限制。

2026-11-17方向与事实引用契约验收后，专用研判专家配置为`max_input_length=128000`、`max_tokens=28000`（包含模型推理输出）。当日只读核验的原生专家模型配置声明上下文200000、输出上限28000。一次冻结输入验收消耗输入58680、输出9120，其中推理7039。单次模型时限120秒，持久轮次期限600秒。更换模型时按其声明上限重新核验。

V3 执行模式向 OCTOP 返回决策所需字段，并保留完整冻结输入供发布验证与审计。规模和 ADV 金额在模型侧使用无损科学计数法，避免被误识别为电话号码。分析 JSON 超过 330,000 UTF-8 字节时使用简要观察信息，仍保留全部板块、候选 ETF、观察代码及事实引用。简要响应仍超限时，明确记录 `NATIVE_ANALYSIS_TOO_LARGE`。

## 配置与发布

生产自动化配置增加`STOCK_HERMES_TRIGGER_MODE=octop`。保留`STOCK_AUTOMATION_HERMES=true`表示业务worker开启。Compose传入`HERMES_TRIGGER_MODE=octop`，API在内部8001端口服务MCP，未映射公网端口。

`data/octop-native.token`为独立业务工具凭据，UID:GID 10001:10001、0600；仅受控业务工具可使用。OCTOP连接器地址为`http://stock-v4-backend.internal:8001/mcp`，Compose持久声明该内网别名，符合OCTOP的内网HTTP地址校验。原工作台token不会交给运行期Agent。

ETF资料维护另外使用独立专家与四个受控工具（维护prepare/status、官方核验context/submit），按10:00主维护、14:15缺项复核执行，低频资料按类型到期更新；手动materials从已存活动方向/持仓关联取得官方静态材料，手动verify回核已导入材料，两者不新增定时或行情采集。materials可指定最多8个target_codes，并在入队时逐只核查真实关联。原研究专家的三工具白名单和五轮定义保持不变。维护周期、预算、失败处理及代码入口见[ETF穿透资料维护](etf-reference-maintenance.md)。

先通过隔离数据库/镜像验收，再选择GitHub已验证的固定镜像摘要发布。安装OCTOP兼容覆盖及专用专家时短暂停止OCTOP；配置通过官方管理服务写入，保留原专家和会话。五轮任务先配置禁用，确认股票系统切入octop模式后统一启用。

## 验证命令

在已配置隔离PostgreSQL、Redis及STAGE2_TEST_ENV_FILE的调测环境：

```bash
python -B -m unittest discover -s tests -p test_native_rounds.py -v
python -B -m unittest discover -s tests -p test_native_mcp.py -v
python -B -m unittest discover -s tests -p test_stage2_automation.py -v
python -B -m unittest discover -s tests -p test_delivery_failure_receipt.py -v
python -B -m unittest discover -s tests -v
```

OCTOP专用测试使用其安装的LangChain/DeepAgents运行时独立执行，不将这些依赖加入股票业务镜像。验收包含实际异步Agent图、非法工具拦截、假成功拒绝、调用预算、原生任务反馈和usage记录。

```bash
python -B -m unittest discover -s tests_octop -v
```

## 结果与边界

股票后端已在隔离调测数据库通过396项测试（其中1项常规迁移跳过，独立迁移测试已另行通过），offline smoke通过，数据库版本0014，前端3项测试通过。OCTOP真实依赖环境中的23项专属测试及覆盖文件导入检查通过；包括完整Harness流程在256节点预算内完成8次模型调用、第9次被拦截。官方MCP探测和三个工具加载已通过真实内网连接验证。镜像升级兼容与线上任务配置结果以本次发布验收记录为准。

生产切换不补造早上缺失轮次，不重发旧报告；任务立即执行时仍检查真实时间和已有轮次。原生任务失败显示具体阶段，模型自己声称完成不能代替已落库回执。

## 2026-11-17 发布失败的受控恢复

`stage2/native_recovery.py`只允许恢复当日已完成采集、已冻结决策但在`PUBLISH`阶段失败的原始轮次。管理员用下面的CLI登记一次授权（有效10分钟），然后在OCTOP原任务点“立即执行”。原研究专家仍只有prepare/submit/status三个工具；CLI本身不启动研判、不发布报告、不发送QQ。

```bash
python -B -m modules.strategy_hermes.stage2 --env-file /private/stage2.env --input-kind LIVE \
  authorize-publication-recovery ORIGINAL_RUN_UUID --calendar /private/calendar.json \
  --operator OPERATOR --reason '已核实发布失败原因并部署修复'
```

原prepare领取授权时，使用原`round_tasks`剩余第2次机会，并在同一事务绑定恢复任务租约。原calls计数不变；恢复只复用原context/decisions，不再采集或调用MX。授权、原失败任务快照和恢复任务分别持久保存；原失败记录、错误和输入保持不变。恢复沿用原报告ID与Outbox唯一键，实际发布时间与原事实截止时间分开显示。重复点击返回同一恢复任务，授权到期或再次失败不重置预算。

授权和执行必须同一交易日09:30–15:00；午间可发布已冻结证据。其他轮次正在执行、已有后续轮次发布、原报告已入账、决策校验失败时拒绝恢复。发布提交前再次核验这些边界。新迁移0016增加独立恢复表与只读合并视图，原native_round_jobs表和唯一键不变，旧镜像原生入队SQL仍兼容。发布/回滚前仍按既有流程暂停领取、收尾worker。

同批修复：执行日历保留已提交历史、只扩未来；执行单位保留旧持仓规则，新候选仍须通过当前核验；无指数权重的目录产品汇总为资料缺口，不放入每个板块的候选名单。QQ与工作台均保留缺口数量和恢复时间。稳定错误码覆盖日历、单位、规则和租约冲突，日志统一读取`error/stage`。

```bash
python -B -m unittest discover -s tests -p test_native_recovery.py -v
python -B -m unittest discover -s tests -p test_execution_units.py -v
python -B -m unittest discover -s tests -p test_round_claim_transaction.py -v
node --test tests/test_data_coverage_ui.mjs
```

0016有数据升级验收额外验证原失败记录全文不变，以及升级后旧`INSERT ... ON CONFLICT(ns,trade_date,slot)`继续创建、去重。实际发布结果与测试数量见对应交付记录。
