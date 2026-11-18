# OCTOP 专属验证

这些测试依赖 OCTOP 镜像内真实 LangChain、Deepagents 和 OCTOP Python 包，与股票后端 `tests/` 分开运行，不向模型或生产业务接口发请求。

MCP 地址固定为 `http://stock-v4-backend.internal:8001/mcp`；backend 的共享 Docker network 必须持久配置此 alias。OCTOP 官方 URL 校验认可 `.internal` 内网后缀，普通无后缀容器名会被当作公网域名要求 HTTPS。原生校验回归覆盖这两个场景。

`max_iters=256` 是 LangGraph 图节点预算，包含各 middleware hook；模型调用仍由专用 guard 限制为每轮最多 8 次。真实 HarnessAgent 回归验证旧 24 节点预算过早触发 GraphRecursionError、256 节点可完成 8 次模型调用、第 9 次调用仍被 guard 拦截。

ETF 资料维护使用独立 prompt、两个维护工具和 mode 校验，模型调用最多 24 次，图节点预算为 768。回归验证 24 次调用可完整结束、第 25 次被拦截，SUCCEEDED/PARTIAL 必须来自当前任务的真实工具终态回执；研究模式继续保持 3 个工具和 8 次模型调用。

在 Linux 主机，将本仓库作为只读目录挂入已审核镜像：

```sh
docker run --rm --network none --entrypoint python \
  -v "$PWD:/review:ro" -w /review \
  ghcr.io/tencentcloud/octop:1.0.2 \
  -B -m unittest discover -s tests_octop -v
```

覆盖三个工具的模型可见与执行边界、8 次模型调用上限、当轮 slot 与已落库报告回执、失败阶段、真实 LangChain/Deepagents 图、超过默认 20,000 token 阈值的大结果完整性、失败与成功各记一次 usage、失败推送并保持异常、官方本地配置签名与暂停调度生命周期、仅专用专家最终关闭 retry/bootstrap/memory/team。

配置部署使用 `tools/configure_octop_native.py --model-ref <已核实模型> --bundle-file <文件>` 生成无凭据 bundle；`tools/prepare_octop_native_patch.py` 根据镜像 ID 和两份原源码 SHA 生成四个只读挂载与原始备份。`tools/octop_native_local.py` 仅在原 OCTOP 容器停止后的一次性容器运行，验证 5 分钟内的停止证明、四文件 manifest、bundle 和 MCP 工具集。默认创建禁用任务；`--enable-cron` 仅用于旧五轮时钟已关闭后的切换。该进程内 cron scheduler 始终暂停，主动关怀定时器不启动。
