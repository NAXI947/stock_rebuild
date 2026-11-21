# stock-v4

A 股板块轮动研究、ETF 模拟交易、手动回测和实盘成交记账。开发基线为 [重构方案 V1.4 开发定稿](docs/重构方案V1.4_开发定稿.md)，约束见 [CLAUDE.md](CLAUDE.md)。

## 运行与调测

| 阶段 | 入口 | 命令与实测记录 |
| --- | --- | --- |
| Stage 1 冻结样本离线回放 | `python -m modules.strategy_hermes` | [验收与运行](docs/Stage1-验收与运行.md)、[旧资产与轮动契约](docs/Stage1-旧资产与轮动契约.md) |
| Stage 2 五轮 Hermes 调度、模拟和通知 | `python -m modules.strategy_hermes.stage2` | [调测与联调](docs/Stage2-调测与联调.md) |
| Stage 3 手动回测 | `python -m modules.strategy_hermes.manual_backtest` | [手动回测调测](docs/Stage3-手动回测调测.md) |
| Stage 4 Web、回测工作台和实盘记账 | `python -m modules.strategy_hermes.workspace_web` | [Web 调测](docs/Stage4-Web调测.md) |

LEFT/RIGHT 支持按交易日全市场批量补采、成功页复用和分块重算。Web 按原备份恢复独立的五类更新页，操作见 [数据更新与补采](docs/data-updates.md)；历史展示见 [LEFT/RIGHT 与热门板块](docs/left-right-hot-sectors.md)。

从五轮研判到买卖的完整链路（AI 的输入和输出、板块状态、ETF 穿透、买入和卖出）见 [Hermes 研判到买卖链路](docs/hermes-研判到买卖链路.md)；当前生效的退出规则见 [hermes_signal_cycle_v2](docs/hermes_signal_cycle_v2.md)。

ETF V3.2 的代表性规则见 [ETF V3.2 调测说明](docs/etf-v3.2.md)。成员数不少于 300 的板块须先登记产业归属才能新开仓，审核口径和发布步骤见 [大板块产业归属登记](docs/etf-taxonomy-registration.md)；产品资料准入见 [产品资料核验](docs/etf-product-verification.md)。

安装 `requirements-stage2.lock` 中的锁定依赖。完整回归使用独立的 PostgreSQL 和 Redis 测试实例，启动配置见 [验收构建](.github/workflows/verify-build.yml)。冻结回放可单独运行：

```bash
python -B -m modules.strategy_hermes.runtime smoke --offline
```

## 构建与发布

向 `main` 推送触发 **Stock V4 Verify and Build**，通过回归与镜像冒烟测试后发布后端、Web 镜像及固定摘要清单。手动运行 **Stock V4 Manual Deploy**，选择一次成功的验收构建，取得批准清单。生产主机在维护窗口执行发布、兼容性校验和健康检查，支持按原镜像回退。发布流程依当前约定省去数据库备份。

生产依赖、命令与启用条件见 [发布操作手册](deploy/README.md)。数据服务凭据、工作台令牌和盘中证据均由运行环境管理。
