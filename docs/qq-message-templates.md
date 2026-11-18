# QQ 消息模板管理

本次按用户提供的 `消息模板优化包-20261116-优化版.zip` 接入五类 QQ 模板。只处理推送展示，不改变 Hermes 研判输入、筛选、数据覆盖放行、模拟交易和任务排期。

## 统一目录

生产编辑目录：`/opt/1panel/apps/local/stock-system/deploy/qq-templates/`。

| 文件 | 用途 | 使用入口 |
| --- | --- | --- |
| research.txt | 五轮研判报告 | 报告落库 → QQ outbox → 模板渲染 |
| maintenance.txt | OCTOP 10:00到期维护、14:15仅待补 | 维护结果落库 → QQ outbox |
| status.txt | 失败、跳过、暂停及原生会话异常 | 原生工具结果/受鉴权回调 → 去重 outbox |
| validation.txt | 人工联调验收 | 使用已核实的测试结果生成验收消息 |
| daily.txt | 投研小助手 QQ 日常答复 | 仅 my-stock 的 QQ 请求临时注入排版说明 |

源文件保存在 `modules/strategy_hermes/qq_templates/`，发布时复制到以上生产目录，镜像内同版文件用于正式推送的格式降级。backend、worker 和 OCTOP 均只读挂载同一生产目录。五个源文件保留附件原文；字段值由程序根据已保存事实生成。

旧 `deploy/qq-brief.md` 和工作区同名文件仅供历史 `octop-markdown-v1` 重试兼容；新消息使用 `qq-template-v2` 和中央目录。

## 修改与行为

保留各文件 `{{字段名}}`，每个字段恰好出现一次；可调整文字、图标、空行以及多字段同行排版。一个模板最大 8 KiB、64 行、每行 300 字符。程序拒绝表达式和未定义字段。

修改目录中的文件后，下一条新消息读取新版本，无需重启。已经开始发送的 outbox 消息保存最终正文和模板 SHA256，重试使用同一正文。正式报告最大 1500 字符，维护最大 1400 字符；超长时按完整板块卡片/条目缩减，并保留详情入口，不截断失效条件。

正式推送模板无效时使用镜像内批准版本并记录 `template_fallback`。历史兼容字段 `workspace_template_invalid` 保留。日常答复模板不可用时保留原会话答复；该排版上下文不写入用户历史，不影响网页会话或原生自动化专家。

`ENABLE_QQ_TASK_NOTICES` 仅在生产 LIVE 运行空间启用；内部 `/native-notice` 使用原生 MCP Bearer 鉴权，绑定八个既有 cron 与专家，拒绝过期/额外字段/任意错误正文。已有正式报告或维护结果时复用通知，后台任务仍活动时标记“原生会话异常”并显示实际状态。

## 验证入口

`python -m unittest discover -s tests -p 'test_qq_*.py' -v`

数据库集成测试使用独立 PostgreSQL/Redis 测试环境及 `STAGE2_TEST_ENV_FILE`；OCTOP 中间件测试用实际依赖、模拟模型与 HTTP，并禁止网络和数据库连接。

部署包括 backend/worker 镜像、统一模板目录及 OCTOP 只读代码挂载。OCTOP 专家设置、模型和八个定时任务保持原配置。发布结果、生产 QQ 回执和未完成项另记交付报告。
