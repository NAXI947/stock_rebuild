# ETF产品首次正式规则核验

目录采集只建立产品身份。首次可交易规则依据官方材料逐字段核验，经受控材料导入和原资料维护worker核对官方内容后，发布追加映射。产品范围为境内被动行业/主题ETF，包含科创板、创业板等产品，不以主板占比审批。实际涨跌幅比例必须来自材料；不统一写成10%。

核验方法为`REVIEWED_OFFICIAL_EVIDENCE_V1`，`reviewed_by`如实记录核验者或`AI-assisted review`及负责人标识；该字段提供审阅追溯，不声明用户已人工签字，也不新增逐只人工前置审批。运维可汇集已核读的官方材料批量导入，导入和维护复核保留各自责任与时间。

## 运行入口

`modules/strategy_hermes/product_verification.py`提供三个运维CLI。运行期OCTOP专家不新增系统命令、SQL或任意文件权限。

```bash
python -B -m modules.strategy_hermes.product_verification --env-file "$PRIVATE_ENV" import review.json --mapping "$MAPPING_FALLBACK" --operator "$OPERATOR"
python -B -m modules.strategy_hermes.product_verification --env-file "$PRIVATE_ENV" status "$REQUEST_ID"
python -B -m modules.strategy_hermes.product_verification --env-file "$PRIVATE_ENV" rebase "$REQUEST_ID" --mapping "$MAPPING_FALLBACK" --operator "$OPERATOR" --reason "其他资料已更新，产品与规则核验内容不变"
```

生产容器已有显式环境变量时可省略`--env-file`。隔离测试环境使用`--input-kind FROZEN`；CLI不提供生产命名空间改写、联网采集、直接放行或强制发布时间选项。执行前通过原迁移入口应用`0017.sql`。CLI读取最新数据库映射，`--mapping`仅作为既有文件后备。

import只校验本地材料、存储官方原文和申请。成功返回PENDING后，可由原10:00/14:15维护回核，也可通过原OCTOP维护工具手动调用`stock_etf_maintenance_prepare({"mode":"verify","request_ids":["申请ID"]})`。verify仅处理最多8份已导入申请的官方静态原文，不采目录、Kaipanla、行情或ETF历史，允许盘外执行；两个原定时窗口保持不变。

verify复用原BackfillQueue与唯一worker、120次请求/12分钟累计活动时间、持久请求账本、缓存、暂停取消和租约。任务绑定申请集合与父版本；同一申请重复触发返回原任务，不重置预算。只允许`maintenance:rule_verification`来源，CLI仍不直采。status持续返回每个ETF的待材料、待审阅、待回核、待发布、已发布、需复核或版本协调状态、申请/目标ID及原因；QQ模板使用同一队列事实。

原market/retry维护在优先回核已导入申请后，按实际活动板块/持仓关联登记未核验目标，并在同预算内准备官方材料。OCTOP维护专家通过`stock_etf_verification_context(target_id)`取得已存证据与review_schema，再调用`stock_etf_verification_submit(target_id, review)`提交逐字段官方审阅；工具绑定身份、版本、取得时间与内容摘要，不接受任意URL/文件。证据不足提交NEEDS_MATERIALS；APPROVE只生成PENDING申请，随后显式verify并检查发布回执。专家仍有24次模型调用上限，未完成目标留在持久队列由下一原维护轮次接续。

## 材料契约

顶层必需字段：

| 字段 | 含义 |
|---|---|
| schema_version | 固定`stock-v4-product-verification-v1` |
| etf_code / index_code | 目录中的ETF和实际跟踪指数，必须与当前映射一致 |
| expected_mapping_version | 导入时审阅的当前映射版本 |
| scope | `domestic_passive_sector`，有证据确认境内被动行业/主题范围 |
| trade_unit | 官方交易单位，正整数 |
| price_limit_ratio | 官方实际比例的精确字符串，例如20%为`"0.2"` |
| settlement_cycle | 规范值`T+1` |
| effective_at | 本次完整规则的生效时间，带时区；未生效材料保持待核 |
| reviewed_by / reviewed_at | 核验人及其实际核验时间，带时区 |
| reason | 本次核验或重新核验说明 |
| documents | 最多4份官方文件，单份8MiB、合计32MiB |
| claims | 对etf_code、scope、index_code、trade_unit、price_limit_ratio、settlement_cycle逐字段提供文档定位及引用 |
| supersedes | 可选；显式替换同产品之前的未发布申请 |

documents的每项包含`url`、`sha256`、`acquired_at`、`effective_at`、`file`。file为材料JSON同目录内相对文件路径；本地实际内容必须与SHA256相同。原文持久保存到PostgreSQL，发布后不按临时TTL清理。文件读取有界且不产生应用临时文件。

URL仅允许HTTPS、标准443端口及现有官方域`sse.com.cn`、`szse.cn`、`csindex.com.cn`、`cnindex.com.cn`和其子域；禁止URL用户信息、片段与HTTP跳转。扩展基金公司官方域需要单独审核精确白名单，不接受任意URL。

下列是结构示意，`<...>`必须替换成真实材料事实，不能直接作为核验材料：

```json
{
  "schema_version": "stock-v4-product-verification-v1",
  "etf_code": "<ETF.SH或ETF.SZ>",
  "expected_mapping_version": "<当前映射版本>",
  "scope": "domestic_passive_sector",
  "index_code": "<实际跟踪指数>",
  "trade_unit": 100,
  "price_limit_ratio": "<实际比例>",
  "settlement_cycle": "T+1",
  "effective_at": "<带时区实际生效时间>",
  "reviewed_at": "<带时区实际核验时间>",
  "reviewed_by": "<核验人>",
  "reason": "<核验说明>",
  "documents": {
    "prospectus": {"url": "<官方原文URL>", "sha256": "<64位真实摘要>", "acquired_at": "<实际取得时间>", "effective_at": "<文件生效时间>", "file": "prospectus.pdf"},
    "trading_rules": {"url": "<官方交易规则URL>", "sha256": "<64位真实摘要>", "acquired_at": "<实际取得时间>", "effective_at": "<文件生效时间>", "file": "trading-rules.pdf"}
  },
  "claims": {
    "etf_code": [{"document":"prospectus","locator":"<页/条款>","excerpt":"<官方原文>"}],
    "scope": [{"document":"prospectus","locator":"<页/条款>","excerpt":"<官方原文>"}],
    "index_code": [{"document":"prospectus","locator":"<页/条款>","excerpt":"<官方原文>"}],
    "trade_unit": [{"document":"trading_rules","locator":"<页/条款>","excerpt":"<官方原文>"}],
    "price_limit_ratio": [{"document":"trading_rules","locator":"<页/条款>","excerpt":"<官方原文>"}],
    "settlement_cycle": [{"document":"trading_rules","locator":"<页/条款>","excerpt":"<官方原文>"}]
  }
}
```

文档内容摘要用于确认来源文件一致；范围和条款含义由`reviewed_by`作显式审核，程序不根据摘要、名称或目录分类推断交易规则。

## 状态、并发与恢复

PENDING进入原维护队列，每次至多受理8产品。原任务先完成REFERENCES，再优先受理产品核验，之后执行常规目录/成员/权重维护；全部共用原120请求、12分钟和盘中窗口，不因目录待补量大而长期饿死首次核验。worker校验目录身份、父版本、规则生效时间和持久原文摘要，然后通过原request预算请求官方文件摘要。成功来源按URL、目标摘要及周次缓存复用。所有文件匹配后记录VERIFIED及实际`verified_at`，规则中保留原`reviewed_at`、`effective_at`、官方取得时间、检查时间、源批次和全部字段引用。

映射发布取得namespace锁，再取得mapping-publish锁；当前父版本仍匹配才插入不可变新映射，并在同一事务将申请置PUBLISHED。核验与常规维护的中间版本最后归并，始终以任务开始读取的已发布映射为父版本。常规维护随后耗尽预算时保留已完成的核验；取消或失去租约仍禁止激活。没有其他资料变化时，核验成功本身也生成新映射版本。重复相同材料返回同一申请；重复映射发布不再次激活。旧版本、旧报告和已成交周期保持原样。

连接或预算失败保持WAITING_DOCUMENTS/PENDING，后续原维护窗口可复用已保存源批次；官方摘要不符进入REVIEW_REQUIRED，须补新文件和引用。父版本变化先比较原不可变映射与当前产品的身份、单位、规则及证据；完全一致才自动安全rebase，发生变化或基准缺失时明确进入人工/AI重新审阅。`rebase`创建带supersedes的新申请并保留旧申请历史。新批准后清除`review_required`，旧失败与旧证据仍留存在请求和映射历史中。

新开仓及待买入成交还会由LIVE准入复核当前发布事实。产品待复核时不产生新买入；已持仓的减仓、清仓继续用周期冻结规则及T+1。资料核验不生成方向、候选、订单或模拟成交。

## 验证

```bash
python -B -m unittest discover -s tests -p test_product_verification.py -v
python -B -m unittest discover -s tests -p test_etf_maintenance.py -v
python -B -m unittest discover -s tests -p test_mapping_maintenance.py -v
```

数据库测试需Linux锁定依赖、隔离PostgreSQL/Redis与`STAGE2_TEST_ENV_FILE`。单元测试里的合成材料仅验证输入契约；正式验收另外保留真实非512800产品的官方材料、申请ID、新映射版本和隔离模拟结果。

## 活动产品的自动材料生产与恢复

`product_materials.prepare_materials`只从已保存活动方向及持仓相关方向选择有真实成分权重或已核验板块—指数关系的待核产品，每轮最多8只。它建立持久`product_verification_targets`（迁移0018），不扫描补齐全部目录，也不新增交易门槛。目录身份、规模、成交额变化不授予交易规则。

默认选择先推进尚未建立材料目标的产品，各组内优先持仓；已有失败目标按上次尝试时间轮转，已待AI审阅或已有活动申请的产品不占新的采集名额。材料目标选择不使用研判展示的前8项或同指数代表产品裁剪，避免固定低代码产品长期占满名额。显式materials任务可冻结最多8个`target_codes`，仍逐一验证当前活动方向的真实关联；同一显式集合在待审、待回核等状态变化后保持不变，供原任务幂等复用。

材料生产在原worker的request/checkpoint内执行。上交所使用官方公告查询一页10条，按实际日期选择目标产品最新招募说明书；深交所使用官方基金公告POST接口，一页10条，分别寻找招募说明书和上市交易公告书。只接受目标代码一致、日期不晚于执行时点的官方结果。`official_product_sources.json`中的URL只是已知官方材料线索，含588100和512480；登记URL不表示该产品已审核。SSE发现失败可沿用已知官方URL，并在材料原因中记录该事实。缺来源或字段依据不足时保存具体待补原因。

原文字节与来源回执存入数据库。HTML/PDF在内存提取带段落/页码定位的文本；PDF最多80页、文本最多240000字符，每份审阅上下文最多8000字符；pypdf固定依赖在镜像构建时安装。未展示的内容不由模型补写，证据不足走明确待补。页面及context读取不产生网络请求。

上海交易规则来源使用上交所现行《交易规则（2026年修订）》官方DOCX，同一份原文包括3.3.13的一般基金10%、6.6的20%基金例外及适用条件、11.9的施行日期。继续保留四份材料上限，不从例外通知推算普通比例。DOCX仅在内存读取word/document.xml，最多256个ZIP成员、4MiB XML，拒绝DTD/实体及不可安全解析内容；仍保留原URL、字节hash、实际取得时间和段落定位。上下文优先展示相关基金交易条款、相邻例外条件与生效条款，比例适用性由OCTOP结合真实产品/指数事实审阅。

每份8000字符内最多为封面和日期原文预留2000字符，保留原页码/段落定位；长页后部的合同生效事实通过原文片段展示，不再只截取页首。合同生效日、说明书内容截止日、网页发布日期和指数数据日期分别解释，不自动转换为规则生效日，也不自动填入审阅结果。

OCTOP通过受控`stock_etf_verification_context(target_id)`读取已存文本，使用`stock_etf_verification_submit(target_id,review)`作逐字段语义审阅。提交只引用已有文档label、定位和原文片段，不提供任意URL/文件、ETF身份或审阅人签名。服务端绑定真实目标、当前映射及时间，审阅人固定记为`OCTOP AI-assisted official evidence review`；校验引用确实出自对应段落，ETF/指数代码、交易单位、涨跌幅数字和T+1与该字段的引用一致。字段数值检查仅是引用绑定，具体条款的适用条件仍由OCTOP明确审阅，不因文档中出现某个百分数就自动批准。

完整证据生成PENDING申请后，再经原`apply_queued`官方回核及原子发布。`NEEDS_MATERIALS`审阅决定保存为REVIEW_REQUIRED并列出具体原因；官方文档新摘要会刷新原目标资料，新文本进入AWAITING_REVIEW供再次审阅。已发现的文档变动会使旧下载缓存失效，仍复用原任务预算。材料准备的预算/时间中断保留目标和已取得文档，不丢弃同任务已经完成的正式核验。

`review_contract`明确两种互斥结构：缺材料仅提交`{"decision":"NEEDS_MATERIALS","reason":"具体缺口"}`；APPROVE必须包含decision、scope、trade_unit、price_limit_ratio、settlement_cycle、effective_at、documents、claims、reason九个字段。scope固定为`domestic_passive_sector`；claims是六个字段名到引用数组的对象，不能写成带field属性的列表。target_id仅放工具参数，ETF/指数身份和审阅身份由服务端绑定。上下文给出完整JSON形状示例，示例数值/日期需用本目标原文核读结果替换；effective_at使用实际生效/适用时间，不能用acquired_at代替法条日期。必要日期依据缺失仍走NEEDS_MATERIALS。

全局映射更新造成父版本落后时，自动恢复先读取原不可变映射，比较目标产品的名称、指数、上市状态、分类、单位、全部规则及证据。仅这些字段完全未变时调用带`supersedes`记录的安全rebase；规模等无关资料可以变化。产品或规则改变、原基准不可读取时保持明确待审。自动rebase沿用外层任务请求预算、保留原申请及尝试历史，不通过新申请重复刷新预算。

`queue_snapshot`返回跨任务持续的材料及申请状态、原因、request_id、target_id、发布版本和次数；汇总读取有界，不因任务结束丢失REVIEW_REQUIRED/NEEDS_REBASE待办。材料原文、核验申请、旧报告及成交记录均保留。

定向验证入口：

```bash
python -B -m unittest discover -s tests -p test_product_materials.py -v
python -B -m unittest discover -s tests -p test_product_verification.py -v
```

## 手动静态材料和回核

在OCTOP维护专家明确执行 `执行ETF资料维护 materials`，可传 `target_codes=["512480.SH"]` 冻结首批实际关联目标；省略时冻结当前已有活动方向选出的最多8只。任务只允许 `maintenance:official_discovery` 和 `maintenance:official_material`，沿用原BackfillQueue、唯一worker、120次请求、累计12分钟、租约、取消和发布暂停，不执行普通参考目录、Kaipanla/行情、ETF历史或产品回核。材料达到AWAITING_REVIEW后，同维护专家取得context、按review_contract核读提交，再prepare verify并等待实际发布回执。

重复materials依当前映射、明确目标/已存方向范围、官方文档已观察失效事实复用原任务及预算；本任务把目标改为待审或待回核，不产生新预算。原常规维护可以在后续窗口公平推进其他目标。verify绑定申请集合与父版本；已绑定和新申请混用时返回existing_run_ids和unbound_request_ids，专家先查原任务，只对未绑定申请继续提交。官方请求已失败时重复verify读取原回执，反馈说明下一原10:00/14:15窗口回核。

维护完成资料核验后若恰逢发布暂停，在最终映射发布门控中返回QUEUED，保留VERIFIED结果、已消费预算和源批次；取消/租约失效仍优先阻止发布。解除暂停由同worker继续原任务。

## OCTOP 参数序列化与可恢复工具错误

OCTOP 当前 native MCP 转换器把数组参数转成字符串。因此对模型公开的 `request_ids`、`target_codes` 使用 JSON 数组字符串，例如 `request_ids='["pv-0123456789abcdef0123456789abcdef01234567"]'`、`target_codes='["512480.SH"]'`。入口兼容单个合法 ID/代码字符串及原有数组客户端，并统一转换为严格列表；非空、唯一、最多8项、真实申请/关联等服务规则保持不变。`verify` 必须显式提供申请 ID，省略、null、ALL、逗号列表及非法 JSON 不会转成默认全量范围。guard 对跨模式 follow-up verify 使用同样格式约束，并只允许本会话已提交回执中的申请。

已知工具的参数或业务校验错误返回正常 MCP `result`，其中 `isError=true`、文本 `state=TOOL_ERROR`、明确错误代码及期望格式；OCTOP 在原24次调用预算内据此纠错。该回执不生成队列任务、QQ状态通知或新预算，修正后读取原任务真实状态。鉴权、外层 JSON、未知方法/工具等协议错误仍保留原处理。维护专家输入/输出上限保持128000/8192，研究工具及定时不变。

## 原材料任务的受控静态来源升级

已有未核验材料目标的官方交易规则或交易指南URL发生受控配置升级时，重复materials先查同一冻结范围的原任务，再核对原目标与当前产品依据。无关产品发布新映射不会重新创建该任务或120次预算。仅PARTIAL/SUCCEEDED、未取消且请求/累计12分钟仍有余量的原任务可在全局队列锁及容量限制内重排；只补指定新URL，不重新做招募书发现或重复读取其他文档。

`result.material_source_upgrade`记录本次指纹、变更清单、请求时间及原状态，`material_source_upgrade_records`保留已使用的来源版本。任务body、request_id、payload_hash、请求账本和累计耗时保持；暂停/恢复的progress及终态均保留这些记录。同一来源版本失败后不再次重排，待下一原维护窗口处理；成功后即使URL差异已消失，依据未变的同范围调用仍返回原任务。新的明确官方文档失效或产品依据变化按新逻辑范围处理。

新文件取得后，更新目标前再次取得namespace事务锁和目标行锁，复核原状态、申请、产品依据及旧文档URL/hash，且该产品没有活动或已发布申请。若HTTP期间已提交语义审阅，则保留原申请和其文档绑定；此次取得的不可变原文字节仍保留，任务不覆盖已进入核验的目标。

升级通知使用`maintenance:<job_id>:source:<fingerprint>`独立outbox事件；旧`maintenance:<job_id>`事件及投递凭据保留。状态接口的`qq.event_key`只对应当前来源版本，旧通知在`qq.previous_notification`标记`is_current_revision=false`，避免旧SENT被显示成本次续补已经通知。定向验证入口为`python -B -m unittest discover -s tests -p test_etf_maintenance.py -v`，覆盖跨映射父版本、并发重复、成功后的幂等、预算/耗时/取消/队列容量、暂停恢复和通知版本。
