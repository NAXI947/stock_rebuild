# Stock V4 五轮自动研判

默认生产 Compose 启用工作台和手动回测。自动研判、行情采集和 QQ 推送各有单独开关。自动研判由已有的唯一 worker 在交易日 09:35、10:35、13:05、14:05、14:30 执行。部署使用生产命名空间，调测任务使用调测命名空间。

## 工作目录与凭据

下列路径以 /opt/1panel/apps/local/stock-system 为根。应在已确认的非交易维护窗口安装，再切换 Compose。先验证 OCTOP 的 my-stock 专家、默认模型和 MX 技能已可调用。

| 生产路径 | 内容 |
| --- | --- |
| data/trading-calendar.json | 经核验且覆盖目标日期的 SSE 交易日历 |
| data/etf-mapping.json | 经核验的板块、ETF 规则及成分映射 |
| automation/feed/tickflow.json | 行情采集开启时使用的 JSON，含 api_key 字段，UID 10001，0600 |
| automation/feed/findesk.json | 可选 Findesk IOPV 凭据 JSON，含 api_key 字段，UID 10001，0600 |
| automation/feed/kaipanla/041_*.json | 行情采集开启时使用的唯一 041 请求捕获，UID 10001，0600 |
| automation/octop-host.env | 宿主服务配置，root 所有，0600 |
| runtime/octop-rpc | 由 root 和 GID 10001 管理的套接字目录，0750 |

automation、automation/feed 及其 kaipanla 目录需供 worker 的 UID 10001 读取，目录权限可设 0750，属主组为 root:10001。工作台只读取 data 下适用的只读文件。OCTOP 宿主服务通过 Unix socket 处理现有 MX、模型研判及指定 QQ 路由协议，凭据留在 OCTOP。宿主配置的 STOCK_V4_OCTOP_CONTAINER 固定为现场 1Panel 管理的 OCTOP 容器名。QQ 启用前，按已核对的私聊路由单独填写 STOCK_V4_QQ_ROUTE_SHA256。容器侧没有 Docker socket 或宿主 SSH 凭据。

## 启用顺序

从同一发布提交取出 deploy/stock-v4-octop.socket、deploy/stock-v4-octop@.service、deploy/octop-stdio.sh、modules/strategy_hermes/stage2/octop_bridge.py 、skills/a-share-hotspot-scout/SKILL.md 和 skills/a-share-hotspot-scout/qq-brief.md。将桥接 Python 文件安装为生产目录下的 deploy/octop_bridge.py，将审核过的板块轮动技能安装为 deploy/a-share-hotspot-scout.md，将 QQ 模板安装为 deploy/qq-brief.md。以上文件属主组 root:root，权限 0644。宿主脚本及 Python 文件由 root 安装并保持仅 root 可写。使用 `sha256sum` 在源工作树和执行机核对宿主脚本、桥接代码、技能和模板的散列值。

```bash
cd /opt/1panel/apps/local/stock-system
install -d -m 750 -o root -g 10001 automation automation/feed automation/feed/kaipanla runtime/octop-rpc
install -m 644 deploy/stock-v4-octop.socket deploy/stock-v4-octop@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stock-v4-octop.socket
systemctl is-active stock-v4-octop.socket
```

使用发布流程合并 deploy/docker-compose.automation.yml，显式设置 STOCK_AUTOMATION_HERMES=true。可先用已经批准的共享行情发布端，设置 STOCK_AUTOMATION_SNAPSHOT_KEY，默认 stock-v4:prod:source:market。使用本 worker 采集时将 STOCK_AUTOMATION_COLLECT 设为 true，先完成来源核验并关闭同一生产行情的另一发布端。使用现有请求预算，每轮批量请求一次 TickFlow 全量 ETF，开盘啦最多五页，来源时间随数据保存。共享 Redis 短时故障时从 PostgreSQL 读取已提交的快照。缺失个别报价和覆盖不足通过轮次质量记录体现。

在 OCTOP 网页的 my-stock 专家设置中修改默认模型，后续轮次会读取已保存设置。默认模型留空或使用自动时，先读取 OCTOP 当前可用的全局默认模型，再按 OCTOP 的已启用模型顺序选择。唯一调度入口在 Stock V4 worker。OCTOP 的定时任务列表不重复注册这五轮。桥接分析优先使用 my-stock 网页已安装的板块轮动 SKILL.md（无效时回退到审核部署副本并标记），记录来源与散列，结合结构化输入，每轮模型研判最多一个 HTTP 请求。MX 由桥接按条件单独检索。经引用核验的研判依据、反证和失效条件保存到报告。应用在提交报告时把已经核验的简报字段写入投递队列。OCTOP 在发送前用 `qq-brief.md` 排版，应用先保存实际正文和模板版本，然后由 OCTOP 的 QQ 通道发送。投递重试复用这份正文。QQ 文本按消息长度选取完整条目，保留 ETF 本轮候选、穿透观察、未通过原因与个股缺口。工作台报告详情可以查看原样正文以及独立的排版与投递状态。

已开启官方行情采集时，可在自动化设置加入 `STOCK_AUTOMATION_FINDESK=true` 启用 Findesk IOPV。每轮在同一采集预算内请求一次，最多 100 只规则已核验的映射 ETF，优先本轮共享快照中已有报价的标的。IOPV 观测时刻及供应商原始时间独立保存，报告穿透展示该项数据和缺口。凭据、行情或有效时间缺失时，其他采集和研判继续。默认保持关闭。首次实盘启用前先按授权供应商凭据验证。

完成回执及接收目标联调后再单独设置 STOCK_AUTOMATION_QQ=true。
工作台的「运行状态」显示 worker 实际生效开关、下五轮计划、最近实际使用的 OCTOP 模型和投递结果。若启用 `STOCK_AUTOMATION_STOCK_RESEARCH=true`，同时开启 Hermes 与正式采集。每轮额外以单次 CN_Equity_A 批量请求获取全市场股票报价，保存共享快照；各板块的有界研究样本从实际行情选择，普涨与 ETF 共振使用完整已知成员及有效报价。个股证据与 ETF 报价各自记录时间及来源。模型只能从本轮已保存的个股证据中选择观察标的。没有通过核验的标的时，报告与 QQ 保留对应状态。缺失指定私聊路由时，桥接端返回失败状态。查看日志时核对每轮状态、数据观测时间、实际请求次数、发布记录以及 Redis、QQ 的独立结果。

```bash
docker logs --since 1h stock-v4-worker
journalctl -u 'stock-v4-octop@*' --since '1 hour ago' --no-pager
```

应用回退沿用同一发布流程，恢复上一版镜像和对应 Compose。新宿主桥接继续使用原 RPC v1，可以与使用该协议的上一版 worker 配合。停止自动化后可以停用 OCTOP 私有 socket。

旧只读专家退役前，先在现有应用上使用新版宿主桥接核对一次授权轮次。确认报告使用 my-stock 模型、报告及 Redis/QQ 结果可查询，再从 OCTOP 网页的「专家」中选定旧专家 stock-v4-stage2-readonly，通过专家操作或编辑页的删除按钮确认。OCTOP 删除该专家的工作区、记忆及会话文件。应用回退时保留新版宿主桥接和审核技能文件。回退到只支持历史文本的 worker 前，检查是否还有未排版的 Markdown 待发条目。恢复兼容版本后可继续处理这些条目。

## 修改简报模板

在 OCTOP 网页「个性化 → 已安装技能」为 my-stock 导入含原 `SKILL.md` 和 `qq-brief.md` 的轮动技能 ZIP，可管理两个文件。模板内的 `{{...}}` 占位符各出现一次；可以调整栏目次序、栏目名称和空行。OCTOP 优先采用专家工作区模板，缺失或校验失败时使用 `deploy/qq-brief.md`。修改影响下次排版，已保存的发送正文继续按原模板版本展示和重试。

## 盘中核对

从已验收提交读取审计脚本，经标准输入在生产 worker 内执行。命令查询既有账本和缓存。

```bash
ssh -T tencent 'cat /opt/workspace/stock-v4/tools/production_live_audit.py' |
  ssh -T telestock 'docker exec -i stock-v4-worker python -B - --day 2026-11-13'
```

在 `rounds` 核对计划时间、观测时间、实际发布时间、请求计数、研判阶段和通知投递。启用 QQ 时，每份已提交的轮次报告入队一次，包括没有模拟节点的轮次。投递失败与研判独立处理。已产生节点但没有发布时间之后的有效行情时，保留待价状态，继续消费正常共享报价。14:30 轮使用当轮采集作研判证据，成交仍需后续有效报价。

## 混合核验版本

2026-11-13 新增 etf-v16-recommendation-v1，静态匹配、动向确认、交易条件分别记录，原自动交易周期继续使用既有规则。完整入口、数据覆盖、低频成员维护和验证命令见 docs/ETF混合核验-20261113.md。盘前/盘后历史统计使用 automation/feed/etf-statistics.json（10001:10001，0600），每300秒处理一只已核验ETF，每日最多三只，各以两次日期区间请求获取日线和复权因子。不要把密钥写进此文档。
