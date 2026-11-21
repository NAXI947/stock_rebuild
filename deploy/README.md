# Stock V4 手动发布

GitHub 的验收构建在提交到 main 后运行测试并发布镜像。手动发布工作流接收验收构建的 run ID 和确认字段 `approval_confirmed`，校验成功的 main 构建并生成待发布清单。此确认用于审批镜像清单。生产操作者核对数据库兼容性，在维护窗口运行发布脚本。

## 首次准备

- 生产目录为 /opt/1panel/apps/local/stock-system。保留 1Panel 管理的 PostgreSQL、Redis、OpenResty 和 OCTOP。应用三个容器加入现有的 1panel-network。
- 保留目录中的私有 .env，权限为 0600。PostgreSQL 使用 postgresql:5432 的 stock_v4_prod，Redis 使用 redis:6379/0，键前缀为 stock-v4:prod:。沿用生产命名空间及凭据。
- 检查 runtime/tmp 下 api、worker、web 和各自的 system、cache 目录。所有这些目录需要 UID:GID 10001:10001 和权限 0700。
- 将已核验的 SSE 交易日历安装到 data/trading-calendar.json，属主为 root:10001，权限 0640。该文件供记账计算 T+1 可卖份额，也用于自动周期的交易日窗口。生产仅接受通过内部验证的日历版本，覆盖期结束前应更新并重建 API 和 worker。
- 将已在私有渠道保存的工作台访问令牌放到 data/workspace.token。文件需要 UID:GID 10001:10001 和权限 0600。令牌为单行 32–128 位字母、数字、下划线或短横线。浏览器工作台使用此令牌鉴权。
- 执行发布的账号需要有生产机 Docker 权限及私有 GHCR 包拉取权限。使用 GitHub 账号和带有 read:packages 权限的令牌，在该账号下用 `docker login ghcr.io --username <账号> --password-stdin` 登录。通过标准输入传入令牌。
- 当前部署启用工作台和手动回测队列。官方采集、Hermes 五轮调度和 QQ 推送均处于关闭状态。正式启用需要生产快照发布来源、交易日历、映射以及 OCTOP 调用和目标完成联调。参见 [自动研判部署](AUTOMATION.md)。

## 校验和发布

从 GitHub Actions 手动发布工作流下载验证产物里的 release.env，并将它传到执行服务器上的私有路径。从同一构建提交取出本目录的 release.sh 和 docker-compose.prod.yml，安装到生产目录下的 deploy/。对后续版本也同时更新这两个文件。

```bash
cd /opt/1panel/apps/local/stock-system
bash deploy/release.sh check /absolute/path/release.env --automation /opt/1panel/apps/local/stock-system/deploy/approved/NAME-automation.env
bash deploy/release.sh apply /absolute/path/release.env --automation /opt/1panel/apps/local/stock-system/deploy/approved/NAME-automation.env
```

已启用自动研判的主机，每次 check 和 apply 都要带 `--automation` 和设置文件的绝对路径，指向最近一次批准的那份。不带时不合并
`docker-compose.automation.yml`：五轮研判、行情采集、QQ、个股研究和 IOPV 全部按关闭启动，触发模式回到 `internal`，
`ETF_REPRESENTATIVENESS_MODE` 回到 `V2`，而健康检查仍然通过。发布后核对 worker 的 `ENABLE_HERMES_SCHEDULE`、
`ENABLE_OFFICIAL_COLLECTION`、`ENABLE_QQ_PUSH`、`HERMES_TRIGGER_MODE` 和 `ETF_REPRESENTATIVENESS_MODE`，
以及 `.stock-v4-releases/<发布编号>/` 下存在 `automation.env`。设置项见 [自动研判部署](AUTOMATION.md)。

自动化设置随发布记录一起保存，rollback 用的是上一条记录自己的设置。上一条记录发布时没带 `--automation`，
rollback 之后自动化同样是关闭的；这种情况改用 apply 重新发布目标版本并带上设置文件。

check 校验清单、令牌文件、交易日历权限、目录、Docker 网络及 Compose 配置。首次 apply 接受既有 .release.env 的默认占位内容，同时检查是否已有同名容器或 Compose 项目。成功发布后，脚本使用 .stock-v4-releases 下的发布清单和对应 Compose 快照作为应用版本记录。

apply 在维护窗口预拉取缺失镜像，写入共享暂停标志，停止接收新的写入和 worker 任务。发布脚本先在旧版 backend 内执行只读等待，检查当前运行空间的共享后台队列（LR 补采、资料更新、ETF 维护）及手动回测队列；后台采集在检查点回到 QUEUED，已运行的有限时手动回测完成后退出 RUNNING。两队列持续空闲 1 秒后，才请求 worker 在至多 150 秒内退出。前置等待上限为 140 秒，查询失败或等待超时会保留暂停标志和运行中的 worker，并中止本次发布，供操作者检查后使用 recover 恢复。

发布暂停与停止信号同时到达时，后台任务优先保存为可恢复的 QUEUED；取消请求仍优先结束任务。恢复领取同一任务时，沿用原请求预算、已用请求数、累计运行时间及持久来源回执，同一来源和范围复用已保存请求。发布流程使用原 worker 和原队列，不增加采集定时或额外预算。

worker 退出状态异常或 API 的在途写请求未排空时，保留暂停并中止迁移。随后单独运行一次向前迁移，再启动并等待三个容器健康检查通过，运行冻结回放及 PostgreSQL、Redis 检查，通过后恢复写入和任务领取。维护期间已经持久化的手动队列状态继续由应用管理。

数据库按增量迁移执行，发布和回退流程都省略数据库备份，符合当前磁盘空间约束。发布前应使用调测库验证有历史数据的升级，以及上一版应用读取升级后的数据库。发布记录保存应用版本对应的镜像摘要和 Compose 文件，仍须保留正在使用的 .env 与其依赖的私有文件。

## 公网 IP 入口

执行服务器的工作台入口为 `http://36.134.112.19:34234/`。Docker 仅将 Web 容器映射到宿主的 `127.0.0.1:34235`。宿主 systemd 将外部 TCP 34234 转发至该本机端口，现有 1Panel OpenResty 继续管理自己的站点。

从本次发布提交取出 deploy 下的 stock-v4-workspace-proxy.socket 和 stock-v4-workspace-proxy.service，在执行服务器安装并启动监听。

```bash
cd /opt/1panel/apps/local/stock-system
install -m 644 deploy/stock-v4-workspace-proxy.{socket,service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stock-v4-workspace-proxy.socket
curl --fail http://127.0.0.1:34234/health
```

首次应用发布完成后再验证 /health。对外 TCP 34234 的放行范围由操作者在防火墙中设置。IP 入口使用 HTTP，浏览器发送的工作台令牌以明文传输。如需加密登录，通过 1Panel 的 HTTPS 站点反代到 `http://stock-v4-web:8080`。

## 回退和恢复

```bash
cd /opt/1panel/apps/local/stock-system
bash deploy/release.sh rollback
```

rollback 使用上一条成功记录的 Compose 和镜像摘要，执行暂停、worker 停止和健康检查，继续使用已经迁移的数据库。发布或回退失败后，脚本尝试恢复上一条成功记录。首次发布失败会停止并移除该次创建的三个应用容器。出现中断或自动恢复失败时，脚本保留暂停状态。检查 Docker 日志、容器退出状态及依赖服务后，在维护窗口执行以下命令恢复最后成功的应用版本。

```bash
bash deploy/release.sh recover
```

发布过程保留镜像，容器日志按 10 MB × 3 轮转。清理镜像前应核对当前和上一版的两个应用镜像摘要及剩余磁盘空间。生产数据库文件与应用运行目录继续由各自的服务管理。
