# Stage 4 Web 工作台调测

运行入口为 `modules.strategy_hermes.workspace_web`。网页与 API 同源，调测服务绑定环回地址，使用独立的 Bearer 访问令牌。样式沿用旧研究页的青绿色、红涨绿跌与紧凑数据表，界面兼容窄屏。读取只访问已保存的 PostgreSQL、报告和本地证据包。

- **轮动与机会**显示五轮报告、1.5 日轨迹、活动板块和持仓关联板块。五轮报告按板块对五个轮次排成矩阵：新增入场、加强、维持、减弱、退潮离场各有符号和颜色，连续相同判断显示累计次数，待确认、证据重复或未覆盖的轮次以虚线标为状态沿用；当日未进入活动方向的板块默认收起，「看原文」切回各轮摘要。矩阵数据来自 overview 的 `round_sectors`，取账本随报告提交的阶段与累计，页面不重算。活动轮动方向按加强、新增、维持、减弱分行，每个方向显示板块名和状态累计（如「减弱×5」），累计多的排在前；板块名取自报告原文，取不到时显示板块标识。报告详情保留发布及观测时间，展示板块判断和 ETF 权重暴露、报价覆盖。
- **回测工作台**包含自动周期与手动组合。可复制已保存周期，也可导入本地证据包或从归档报价选择 ETF 与时点。可编辑、合并 ETF 报价、另存修订、提交或取消，查看份额、T+1 顺延、现金及收益。三日触达从独立证据文件读取。
- **实盘记账**记录已发生的真实成交。迁移 0009 新建独立账本，按请求 ID 幂等。只在已有交易日历和持仓证据支持时给出可卖份额和成本。该入口与模拟订单独立。
- **市场背景**显示已保存快照的板块分布、ETF 报价数量、涨跌和观测时间。更新使用页面刷新。

## 本地运行

先在调测服务器为服务账号准备私有令牌文件，文件模式为 0600。队列需要同命名空间的既有 `serve --manual-queue`。从操作机用 SSH 端口转发访问环回端口。

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
"$runtime/stage2-venv/bin/python" -B -m modules.strategy_hermes.workspace_web \
  --env-file "$runtime/.env.development" --token-file "$runtime/secrets/manual-api.token" \
  --calendar "$runtime/stage2-derived/calendar-729bbab2fc348decbf6af336.json" \
  --market-file "$runtime/stage2-derived/tickflow-kpl-61e3ec272c041efe5fa5a94d.json"
```

## 验收和后续

浏览器通过 SSH 环回预览验证登录、回测工作台、实盘记账表单和市场背景。回测显示冻结结果 9.50% 及四笔成交，市场页从已保存快照读取 243 个板块与 1,634 条 ETF 报价。测试库验证记账鉴权、重复提交、冲突、T+1、已存报价读取和模拟账本隔离。59 项回归测试通过。调测预览会话已结束。

## 首发收尾

- 公网 IP 入口为 `http://36.134.112.19:34234/`。防火墙由操作者管理。通过非安全 HTTP 上下文验证了登录、创建回测节点和复制重试，请求 ID 继续满足服务端幂等契约。工作台令牌经 HTTP 明文传输。
- 正式部署使用 Tushare 已入库的 SSE 日历，覆盖 2026-10-05 至 2026-12-31。导出入口为 `python -m modules.strategy_hermes.stage2.export_calendar`，输入已存档的 `trade_cal` 批次 ID，文件经 `temp_manager` 发布。2026-11-13 的下一交易日为 11 月 16 日。日历覆盖期结束前更新生产文件。
- 手动回测可从共同的报告与报价时间线选点，对比最多三份已保存方案。自动页默认展示近 20 个交易日内实际完成首笔买入的周期，历史持仓仍可查看。毛收益和净收益的胜率各自统计。
- 市场快照与 WS 入口分别为 `/api/v2/market/snapshot`、`/api/v2/market/ws-ticket`、`/api/v2/market/ws`。票据一次使用。通知来源为已提交快照和报告，断线后按 Redis 游标补读，超出保留窗口时读取持久快照。页面刷新和连接操作只读已有数据。
- 运行状态显示轮次、未投递通知和临时盘预算。市场背景显示 ETF 快照成交额。实盘记账下方可读取已保存的 LEFT/RIGHT 盘后档案，配置文件 `data/left-right-eod.json` 使用 `stock-v4-left-right-eod-v1`，包含 `trade_date`、带时区的 `observed_at`、`source`、`items`，每条包含 `code`、`side`、`note`。盘后档案由 `modules.strategy_hermes.stage2.left_right_eod` 离线生成。
- 生产自动研判使用显式发布开关，默认关闭。安装、接入和回退见 [自动研判部署](../deploy/AUTOMATION.md)。Findesk IOPV 和分红及份额事件的正式输入另行核对。符合条件的真实自动成交和三日触达随下一次盘中验收。

## 盘后档案复现

在调测机使用已保存的 Tushare 数据和交易日历，按实际采集可得时间生成盘后参考。

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
python="$runtime/stage2-venv/bin/python"
"$python" -B -m modules.strategy_hermes.stage2.left_right_eod \
  --day 2026-11-12 --as-of 2026-11-13T15:53:03+08:00 \
  --calendar "$runtime/stage2-derived/calendar-729bbab2fc348decbf6af336.json" \
  --env-file "$runtime/.env.test" --code 000001.SZ \
  --tmp "$runtime/tmp" --output-dir "$runtime/stage2-derived"
"$python" -B -m modules.strategy_hermes.stage2.macro_background \
  --capture "/opt/workspace/legacy_backup/kaipanlajson/072_全球指数.json" \
  --tmp "$runtime/tmp" --output-dir "$runtime/stage2-derived"
```

`000001.SZ` 在 2026-11-12 收盘价为 11.79 元，RIGHT 得分为 76.7060，LEFT 缺少完整周线和板块五日收益。四个 Tushare 批次分别保存 91 行日线、复权因子、每日指标和筹码成本。回看 11 月 12 日 23:59 的本地可得状态时没有该日收盘资料。网页入口 `data/left-right-eod.json` 只读取选定档案，暂无更新时保留原日期。设置 `STOCK_LEFT_RIGHT_EOD_CODES` 为逗号分隔的明确股票代码后，同一 worker 从交易日北京时间 15:35 起每 5 分钟检查已入库盘后数据。未达到可计算条件时保留上一份档案。已发布的数据库档案和只读文件按日期择新。盘后任务不请求供应商，不推送 QQ。

全球市场背景可读取 `data/market-background.json`。当前离线样例包括 18 条指数、股指期货、商品和汇率资料，开盘啦 072 回包未给出源观测时间，页面会标明样例和未知时间。该资料按历史样例展示。选择正式背景源时须保留各市场原始观测时间。

正式发布按已确认的无数据库备份约定执行，保留数据库迁移校验和应用镜像回退。部署与操作命令见 [发布手册](../deploy/README.md)。
