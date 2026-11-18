# LEFT/RIGHT 盘后功能恢复

本次恢复用户确认的旧版评分、两类结构筛选和近期趋势。评分公式、权重及 `left_v3.1.2_adj` / `right_v3.1.2_adj` 版本保持原样；筛选使用旧 `structure_watch_v2` 条件。

## 已核实问题

- 2026-11-16 生产归档每侧 5,572 只股票全部 MISSING。共享日线仅覆盖 2026-09-01 至 2026-11-16 的 49 个交易日，多数股票仅有 11 个完整周，历史预热不足。
- 开盘啦 042 的 Count 是本页条数，迁移采集器将其当作总数，每日只保存首 60 条。连续五日板块收益因此缺项。
- 开盘啦 046 有 4 个板块实际成员超过 2000，17 组历史成员缺少第三页；迁移版两页上限及总数硬限制截断了这些关系。恢复旧采集器 10 页上限，按真实 Count 取满即停止，超出上限明确保留部分结果。
- 页面遗漏整理后刚转强、回踩后再转强及详情近 30 个交易日的评分和价格曲线。
- 当全部评分缺失时，更新回执仍按归档写入成功显示 OK；左右模型的缺项理由也被混合展示。
- 本轮曾将日线预热缩窄至 80 个交易日，现已撤回。2026-09-21 锚点评分实查发现 126 只成熟上市股票在近期停牌后不足 80 根实际K线，输入跨入旧预热残留的 2026-01-26 日线；该日历史任务中断时尚未采集 daily_basic，导致这些股票缺换手率。交易所开市日数量与个股实际K线数量不同，补齐采用原有完整 280 天范围。

## 修复入口

- `modules/strategy_hermes/structure_screen.py`：迁移旧筛选纯函数及原参数。
- `modules/strategy_hermes/left_right_views.py`、`left_right_info.py`、`workspace_web.py`：完整股票池筛选、分数降序分页、交易日历对齐的双侧评分历史。
- `modules/strategy_hermes/left_right_backfill_collectors.py`、`hot_sectors_info.py`：042 连续分页与完整性展示，兼容已有首屏资料。
- `left_right_backfill_collectors.py`、`stage2/left_right_eod.py`：046 真实总数、最多 10 页（每页 1000）的成员完整性联动；单任务 12000 次请求预算保持不变。
- `modules/strategy_hermes/bulk_eod_collect.py`：恢复首个目标日前 280 天至最后目标日的所有开市日，采集日线、复权因子、日线指标和筹码成本；周线完整周规则、成功批次复用及请求预算保持原样。
- `modules/strategy_hermes/stage2/left_right_eod.py`：分别保存左右评分的真实缺项。
- `modules/strategy_hermes/data_updates.py`：归档数量与有效评分分别验收。
- `web/app.js`、`web/style.css`：筛选、参数、双分说明、趋势及明细。

原始榜按所选 LEFT/RIGHT 分数降序。两类转强结果按用户本次明确要求，改用 RIGHT 结构分降序，分数相同时按代码排序。旧形态页的板块/确认优先级排序不沿用。

趋势展示最近 30 个交易日的 LEFT、RIGHT、收盘价和涨跌幅，缺数及评分版本切换保留断线。0 分作为有效数值。

## 运行与验收

```bash
python -B -m modules.strategy_hermes.runtime api
python -B -m modules.strategy_hermes.runtime worker
STAGE2_TEST_ENV_FILE=/absolute/private/test.env python -B -m unittest discover -s tests -q
node --check web/app.js
node --test tests/test_left_right_ui.mjs
```

真实历史补齐通过原持久队列执行，单 worker、原请求预算、成功页复用和暂停恢复规则继续生效。重算仅更新盘后研究评分及来源记录。

调测服务器已验证：首轮完整回归 717 项，716 项通过、迁移项在全套中跳过且已单独通过；随后采集窗口与逐股时间边界增补后的针对性回归 50 项全部通过。发布流水线完整回归 722 项，721 项通过、迁移项另行单独通过。前端 DOM/行为回归 15 项通过，覆盖形态无匹配与暂无已保存评分的不同提示；桌面与 390px 手机测试无页面横向溢出和控制台错误。冻结回放收益仍为 9.5000%。

撤回此前“851 次降到 367 次”的预热缩窄。无停牌的冻结样例保持原输入和评分对照；新增停牌场景检查完整 280 天内能取得 80 根实际K线，并验证已完成批次再次执行时不新增外部请求。请求数减少不得以丢失个股实际历史窗口为代价。

真实数据修复需按日期顺序完成后台补齐与评分，覆盖近 30 个展示交易日及首日筛选需要的前 5 个评分交易日。正式发布标识、实际评分覆盖和生产接口验收结果随本次交付记录保存。

046 容量补充后，调测服务器 LEFT/RIGHT 回归 75 项通过，EOD 适配 18 项单独通过，冻结回放仍为 9.5000%；新增案例覆盖 2292 成员三页完整、缺中页、最后允许的分页和超过旧上限的部分结果。
