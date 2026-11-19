# ETF V3.2 调测说明

开发基线为 `main@1ea1d76fea907d56b4a3cd7b2267b83bd33dc230`，实现分支为 `feat/etf-v3-2`。实施参数版本为 `etf-representativeness-v1`。策略阈值在研判准备时冻结。默认模式为 `V2`，调测可分别设定 `V3_SHADOW` 和 `V3_ENFORCE`。

| 环节 | 代码入口 |
|---|---|
| 准备和选择 | `stage2/octop_analyst.py`、`stage2/penetration.py`、`stage2/research_facts.py` |
| 代表性与聚合 | `stage2/etf_representativeness.py`、`stage2/etf_support_facts.py` |
| 建周期和发布 | `stage2/worker.py`、`stage2/postgres.py`、`ledger.py` |
| 资料推进 | `stage2/mapping_maintenance.py`、`stage2/reference_policy.py`、`product_materials.py`、`product_verification.py` |
| 核验和归属发布 | `tools/replay_etf_v32.py`、`tools/publish_etf_taxonomy.py` |

以上未带目录前缀的业务文件位于 `modules/strategy_hermes/`。

国证创业板指和国证生物医药的指数身份保留各自代码。权重通过现有 Tushare `index_weight` 请求入口，基于选中日期、标识、成分唯一性、样本数量及总权重容差核验。官方编制说明提供 100 与 30 的样本数量依据。资料不足时保留已知部分或现有验证版本。 [创业板指编制说明](https://www.cnindex.com.cn/docs/gz_399006.pdf) · [国证生物医药编制说明](https://www.cnindex.com.cn/docs/gz_399441.pdf)

维护仍在 10:00 和 14:15 既有槽位运行，每个逻辑任务沿用 120 次请求和 12 分钟预算。材料线索有版本与目标记录，经营范围和交易规则通过原产品六字段核验发布。未知权重和成员缺口继续显示并分项补齐。

## 调测命令

```bash
# 在隔离 PostgreSQL/Redis 环境设置仓库 CI 所用的测试环境变量。
python -B -m unittest discover -s tests -q
python -B -m modules.strategy_hermes.runtime smoke --offline
node --check web/app.js
node --test tests/test_data_coverage_ui.mjs tests/test_left_right_ui.mjs
python -B -m tools.replay_etf_v32 --test-env /absolute/test.env --archive /absolute/frozen-rounds.jsonl.gz
```

回放输入为相同的已发布研判任务快照与真实模型提交，包含当时使用的映射和行情。输出对照 V2、影子模式与执行模式的方向、推荐、周期和通知队列。回放命名空间单独生成，通知只写入隔离队列。

产业归属由经核验的 `version` 和 `rows` 组成。每行包括 `sector_id`、`index_code`、`family_id`、`canonical_branch_id`、`available_at` 和 `evidence_refs`。导入按原映射发布事务执行。

```bash
python -B -m tools.publish_etf_taxonomy --environment-file /absolute/test.env \
  --mapping-file /absolute/etf-mapping.json --taxonomy-file /absolute/reviewed-taxonomy.json
```

## 生产启用边界

生产正式发布使用经审核的固定镜像。在发布脚本 `--automation` 指定的设置文件中配置 `ETF_REPRESENTATIVENESS_MODE`，该模式会随镜像摘要和 Compose 文件一起保存。先观察 `V3_SHADOW` 的生产差异和数据覆盖，再选择维护窗口为新准备轮次发布 `V3_ENFORCE`。回退恢复上一发布版本所记录的模式，旧设置文件缺少该字段时按 `V2` 处理。已冻结轮次与在途周期沿其保存的版本继续处理。产业归属由有来源的已核验记录逐步发布。
