# 板块与 ETF 信息页

新增独立侧栏“板块与 ETF”，保留“大盘全景”。板块列表和已采集 ETF 行情提供名称/代码搜索、排序、20 条分页、行情来源及逐条观测时间，并展示最新已保存报告中的明确关联。

代码入口：`web/app.js::renderSectorEtf`；`modules/strategy_hermes/sector_etf_info.py::read_info`；鉴权只读接口 `GET /api/v2/workspace/sector-etf-info`。
接口参数：kind=sectors|etfs、q、sort、page、page_size（最多100）。ETF 名称读取现有供应商目录，缺失时使用已配置映射；运行入口支持 APP_ETF_INFO_MAPPING_FILE（默认 /app/data/etf-mapping.json），调测 CLI 使用 --mapping-file。

页面只读取共享快照、供应商目录、已保存报告及映射文件；不新增采集任务，不修改大盘全景刷新频率。市场背景和全球指数后台约15分钟更新一次，市场成交按09:35、10:35、11:35、13:05、14:05、15:00时段展示，后台约15分钟检查到期数据。浏览器进入页面、点击刷新行情或收到共享行情推送时读取已保存数据。

验证命令（已配置隔离 PostgreSQL/Redis 测试环境）：

```sh
STAGE2_MIGRATION_ENV_FILE="$STAGE2_TEST_ENV_FILE" python -B -m unittest discover -s tests -p test_migration_upgrade.py -v
python -B -m unittest discover -s tests -v
python -B -m modules.strategy_hermes.runtime smoke --offline
node --check web/app.js
```

2026-11-14 调测结果：迁移测试1项通过；完整集合241项，240通过、迁移项因已单独运行跳过；离线 smoke OK。新增17项只读接口测试覆盖缺失/零值、Decimal排序、搜索、分页、报告关联和元数据异常。浏览器1440及390宽度检查通过，包括真实快照243板块/1634只ETF、搜索排序分页、单位、来源时间、大盘全景保留和无横向溢出。合成数据另验证搜索响应竞争、推送时保留筛选和焦点、退出登录竞争。

当前真实目录尚缺大部分 ETF 名称；已有映射能补齐2只，其余显示代码及“名称待补”。关联只展示最新已存报告的明确结果。本次未新增全量名称补采或映射重算。

上线核验还发现运行状态读取的旧1 KiB上限拒绝了包含大盘采集详情的合法1,453字节心跳。`workspace_readiness.py`将读取上限改为16 KiB；新增6项测试覆盖实际结构、字节边界、时间和布尔类型，保持字段白名单及180秒TTL。

## 穿透概览（2026-11-21）

页面顶部新增“穿透概览”，用于判断板块穿透 ETF 的映射是否在正常更新、何时需要补采或修正。
代码入口：`web/app.js::sectorEtfPenetration`；`modules/strategy_hermes/sector_etf_info.py::read_penetration`；鉴权只读接口 `GET /api/v2/workspace/sector-etf-penetration`（无参数）。

- 当前穿透 ETF：最新已保存报告的 `input_meta.penetration` 中与本轮板块成分股对上的 ETF，按只数去重；拆成可入场（即下方“已存报告关联”）、仅观察、资料不可用（带拒绝原因）。
- 完整穿透 ETF：最新 `maintained_mappings` 版本里，跟踪指数有可用权重且权重成分与某个板块成分股有交集的 ETF；判定复用 `stage2/penetration.py` 的成分股与权重有效性规则。缺权重的 ETF 不计入。
- 近期新增：最近 12 个映射版本相邻两版之间新增的可穿透 ETF、板块和权重指数，带版本发布时间。
- 缺口：缺权重的指数（按影响 ETF 数排序，最多列 40 个）、映射中没有成分股的板块、最新报告里穿透为空的板块（标出映射是否已补）、权重日期、最近一次维护的状态与待补项。

只读已保存的报告和映射版本，不新增采集任务或调度，不写库。映射版本不可变，单个进程内每个版本只解析一次。
