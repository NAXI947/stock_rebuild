# ETF V3.2 调测说明

开发基线为 `1ea1d76fea907d56b4a3cd7b2267b83bd33dc230`，实现已并入 `main`（`ETFv1.0` 为 `7ad5d89`，`ETFv1.1` 为 `4dba6bd`，验收整改提交为 `2d027f9`）；该基线提交不在 `main` 的历史中，原实现分支 `feat/etf-v3-2` 未保留在远端。实施参数版本为 `etf-representativeness-v1`。策略阈值在研判准备时冻结。默认模式为 `V2`，调测可分别设定 `V3_SHADOW` 和 `V3_ENFORCE`。

| 环节 | 代码入口 |
|---|---|
| 准备和选择 | `stage2/octop_analyst.py`、`stage2/penetration.py`、`stage2/research_facts.py` |
| 代表性与聚合 | `stage2/etf_representativeness.py`、`stage2/etf_support_facts.py` |
| 建周期和发布 | `stage2/worker.py`、`stage2/postgres.py`、`ledger.py` |
| 资料推进 | `stage2/mapping_maintenance.py`、`stage2/reference_policy.py`、`product_materials.py`、`product_verification.py` |
| 核验和归属发布 | `tools/replay_etf_v32.py`、`tools/publish_etf_taxonomy.py` |

以上未带目录前缀的业务文件位于 `modules/strategy_hermes/`。

国证创业板指和国证生物医药的指数身份保留各自代码。权重通过现有 Tushare `index_weight` 请求入口，基于选中日期、标识、成分唯一性、样本数量及总权重容差核验。官方编制说明提供 100 与 30 的样本数量依据。资料不足时保留已知部分或现有验证版本。`absolute_weight_proof` 只在本批数据支持时写入：成份集合已按官方集合或样本数量核验完整；或官方集合、样本数量显示本批缺少成份，且已知权重合计低于 1 减容差。证明内保存依据类型、行数、官方数量和权重合计。无官方参照、子集合计已达 1、或含官方集合之外的成份时不写入，配对按上界未证明处理。 [创业板指编制说明](https://www.cnindex.com.cn/docs/gz_399006.pdf) · [国证生物医药编制说明](https://www.cnindex.com.cn/docs/gz_399441.pdf)

维护仍在 10:00 和 14:15 既有槽位运行，每个逻辑任务沿用 120 次请求和 12 分钟预算。指数权重来源对同一请求窗口在两个不同交易日都成功返回空结果时，记为 `CONFIRMED_SOURCE_MISSING` 并保存首次空结果日期，当日缺项复核不再请求，次日仍在尝试上限内复查；单次空结果仍是 `NOT_PUBLISHED` 并保留当日复核机会。材料线索有版本与目标记录，经营范围和交易规则通过原产品六字段核验发布。未知权重和成员缺口继续显示并分项补齐。

## 调测命令

```bash
# 在隔离 PostgreSQL/Redis 环境设置仓库 CI 所用的测试环境变量。
python -B -m unittest discover -s tests -q
# T01–T32 及补充边界验收；与上一条共用隔离队列，须在其后顺序执行。
PYTHONPATH=tests python -B -m unittest tools.etf_v32_acceptance -q
python -B -m modules.strategy_hermes.runtime smoke --offline
node --check web/app.js
node --test tests/test_data_coverage_ui.mjs tests/test_left_right_ui.mjs
python -B -m tools.replay_etf_v32 --test-env /absolute/test.env --archive /absolute/frozen-rounds.jsonl.gz
```

报告已保存的配对分组为空的板块，在 QQ 简报和工作台统一显示「当前资料范围未找到」；两处读取同一份已保存结果，不各自重算。

回放输入为相同的已发布研判任务快照与真实模型提交，包含当时使用的映射和行情。输出对照 V2、影子模式与执行模式的方向、推荐、周期和通知队列。回放命名空间单独生成，通知只写入隔离队列。

产业归属由经核验的 `version` 和 `rows` 组成。每行包括 `sector_id`、`index_code`、`family_id`、`canonical_branch_id`、`available_at` 和 `evidence_refs`。导入按原映射发布事务执行。

```bash
python -B -m tools.publish_etf_taxonomy --environment-file /absolute/test.env \
  --mapping-file /absolute/etf-mapping.json --taxonomy-file /absolute/reviewed-taxonomy.json
```

## 大板块入场约束（etf-representativeness-v2）

2026-11-20 用户授权，按「先避免亏损、宁可少做」收紧新开仓。成员数不少于 300 的板块，成员重合度分不开对口与不对口的指数：
生产 11-20 10:35 的报告里，算力板块（1,026 只成员）对中证全指家用电器的暴露是 70.6%，对中证计算机主题是 78.2%。
这类板块的配对只有在已发布的产业归属（带依据、已到生效时间）或显式关联存在时才允许新开仓，否则状态为
`BROAD_SECTOR_UNATTRIBUTED`，仍作为观察项显示。参数 `broad_sector_min_members=300` 随策略一起冻结；
此前冻结的策略没有该参数，回放结果不变。只影响新开仓，减仓、清仓和已有周期沿用各自冻结的规则。提示词文本未变，仍为
`etf-representativeness-v1`。

活跃板块里已登记产业归属的指数，取权重时与显式关联同级，不排在普通轮换里等待；否则登记后迟迟没有权重，大板块就一直没有可新开仓的产品。

## 生产启用边界

生产正式发布使用经审核的固定镜像。在发布脚本 `--automation` 指定的设置文件中配置 `ETF_REPRESENTATIVENESS_MODE`，该模式会随镜像摘要和 Compose 文件一起保存。先观察 `V3_SHADOW` 的生产差异和数据覆盖，再选择维护窗口为新准备轮次发布 `V3_ENFORCE`。回退恢复上一发布版本所记录的模式，旧设置文件缺少该字段时按 `V2` 处理。已冻结轮次与在途周期沿其保存的版本继续处理。产业归属由有来源的已核验记录逐步发布。
