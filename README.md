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

安装 `requirements-stage2.lock` 中的锁定依赖。完整回归使用独立的 PostgreSQL 和 Redis 测试实例，启动配置见 [验收构建](.github/workflows/verify-build.yml)。冻结回放可单独运行：

```bash
python -B -m modules.strategy_hermes.runtime smoke --offline
```

## 构建与发布

向 `main` 推送触发 **Stock V4 Verify and Build**，通过回归与镜像冒烟测试后发布后端、Web 镜像及固定摘要清单。手动运行 **Stock V4 Manual Deploy**，选择一次成功的验收构建，取得批准清单。生产主机在维护窗口执行发布、兼容性校验和健康检查，支持按原镜像回退。发布流程依当前约定省去数据库备份。

生产依赖、命令与启用条件见 [发布操作手册](deploy/README.md)。数据服务凭据、工作台令牌和盘中证据均由运行环境管理。
