# Stage 1：旧资产筛选与轮动状态契约

依据：V1.4 第二章、§4.7、§5.1.1、§8.1、附录 B/C/D；轮动状态口径按本次确认。代码在调测机 /opt/workspace/stock-v4。样本标为 FROZEN，只用于离线回放。

| 来源 | 处理 | 实际落点 |
| --- | --- | --- |
| 05-Hermes技能/.../a-share-hotspot-scout/schemas/hermes_sector_rotation_v1.schema.json | 逐字节保留；与旧文件 cmp 一致 | modules/strategy_hermes/contracts/hermes_sector_rotation_v1.schema.json |
| 同技能 scripts/build_rotation_json.py、references/output-and-state.md、scripts/chain_state.py | 参考已发布字段、撤回语义、证据和连续轮次；导出器的计划时间、时效封锁、回源、运行期工具与磁盘写入没有移植 | modules/strategy_hermes/adapter.py、rotation_chain.py |
| 同技能 tests/test_json_schemas.py 的 test_actual_report_producer_and_published_contract | 用旧导出器的 build 函数实际生成 PARTIAL 样本，冻结时钟和 ID；保留原始 JSON。旧样本没有可核验板块 ID，作为观察记录保存 | tests/fixtures/legacy_report_partial.json |
| 03-Hermes板块ETF策略旧规则参考 | 核对 WEAKENING、support_withdrawn、穿透排序语义。当前消费冻结候选和映射版本；公式、阈值、穿透实际执行留 Stage 2 | replay bundle 的 candidates；实时报价与映射不回源 |
| 采集、技能运行时、盘后 LR、前端、WebSocket、QQ、旧数据库 | 在对应阶段按消费者筛选；本阶段没有迁移这些运行入口 | 保留在 legacy_backup |

旧报告契约原样保存。新增独立离线输入契约 modules/strategy_hermes/contracts/stock_v4_frozen_replay_v1.schema.json。运行时检查信号必要字段，单条失效板块/候选记诊断；不以整份旧 JSON 的可选字段作为交易总开关。report_json 是原始文本字符串。适配版本、来源、原行、原信号、推导状态、发布时间和行情观测时间分开保存。

| 状态 | 可执行语义 | 轮动链 |
| --- | --- | --- |
| NEW 新增 | 明确新增，且冻结穿透候选可识别时可生成 BUY | 建立方向；退潮后须用新证据明确新增才可重新入链 |
| MAINTAIN 维持 | 保持状态，保留待执行订单 | 连续有效维持按实际次数显示 |
| STRENGTHEN 加强 | 明确加强；已有 ETF 周期不加仓，已入链方向首次命中的有效候选可入场 | 连续有效加强按实际次数显示 |
| WEAKEN 减弱 | 已成交周期只冻结一次减仓目标；未成交买单取消 | 方向仍留在链中，连续有效减弱按实际次数显示 |
| RETREAT 退潮 | 已成交周期清余仓，覆盖未完成减仓；未成交买单取消 | 立即移出活动 rotation；退潮保留在 retired_directions 和报告档案 |
| UNKNOWN 待确认 | 不生成新节点，待执行订单按原规则保留 | 缺轮、不足、冲突和旧撤回保留观察；不改变轮动成员身份 |

旧版减弱、撤回的 signal 都是 WEAKENING。减弱须由旧结构化 observation_state 精确确认。明确退潮使用独立 judgments[]：phase=RETREAT，sector_id 匹配该报告板块，evidence_ref 匹配该板块 sources；旧状态必须兼容（WEAKENING 且原状态缺失或为撤回），裸撤回、减弱和正文中的“退潮”字样都不会直接清仓。Stage 2 的已发布判断应明确输出这一结构化字段并绑定真实证据。

维持、加强、减弱共用基础状态和连续次数字段。输出以 phase 保存基础状态，以 streak 保存次数，label 生成“加强×3”“减弱×2”等显示。有效状态切换时从 1 起算，缺轮保留最后有效观察，重复证据沿用已有次数。去重优先使用旧报告 research_context.evidence_key；没有时用板块 ID、data_as_of 和来源集合组合。退潮历史与持仓结算分开：当天从链上移出，ETF 账本保留到价格和批次 T+1 条件同时满足。主关联板块在入场时按冻结候选排序确定，其他板块退潮不替代主关联判断。

回放按带时区的实际发布时间与行情接收时间推进；撮合使用严格晚于节点的合法交易时段报价观测时间。报价晚到不覆盖更新快照。需要改写已经提交的事件顺序时新建研究 namespace。CNY、FEN 转成精确 Decimal 元，账本金额按分四舍五入；批次 FIFO、交易单位取整；明确配置完整成本才算净收益。待价格、待 T+1、待交易日历及不足一手分别显示。冻结日历、候选和报价均来自输入。

供 Stage 2 加载的 Hermes 技能已同步这套口径，见 [a-share-hotspot-scout](../skills/a-share-hotspot-scout/SKILL.md)。旧 Stage 1 活动链读取时从已提交的报告恢复此前缺失的维持、减弱计数。
