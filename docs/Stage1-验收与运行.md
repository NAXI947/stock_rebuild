# Stage 1 离线交付

在调测机 106.53.186.201 的 /opt/workspace/stock-v4 运行。使用 Python 3.12 标准库，无需安装第三方依赖，不连接行情供应商或交易账户。参考成交和收益只代表冻结样本。执行服务器未部署此代码。

## 入口与持久目录

- 入口：python3 -B -m modules.strategy_hermes。
- 报告适配及契约：modules/strategy_hermes/adapter.py、contracts/；旧资产与口径见 [Stage1-旧资产与轮动契约](Stage1-旧资产与轮动契约.md)。
- 轮动状态、订单、账本：rotation_chain.py、signal_cycle.py、simulator.py、ledger.py。
- 临时文件：shared_kernel/temp_manager.py。CLI 启动时核查残留；任务租约和目录锁保护正在使用的文件。单任务、总量、条目数、低磁盘保护均在写入路径生效。Stage 1 没有 SDK、HTTP 下载或定时 worker。
- CLI 使用独立 SQLite WAL 持久账本，外部运行目录下按交易事务提交报告、原始证据、轮动状态、持仓、订单及成交。SQLite 临时表在内存，数据库与日志文件不由临时清理器管理。PostgreSQL 服务端账本和 Alembic 待后续迁移验证。
- 执行规则随周期冻结；同一 namespace、同一 ETF 只允许一个活动周期，使用事务和部分唯一索引。相同事件 ID 内容一致时可重试；内容变化或新增旧批次报错，研究重排用新 namespace。明确退潮移出活动轮动链，清仓仍按批次和合法价格执行。

## 复现命令

```bash
cd /opt/workspace/stock-v4
runtime=/opt/workspace/stock-v4-runtime
install -d -m 0700 "$runtime/stage1-demo-ledger" "$runtime/test-tmp/stage1-tests"
export APP_TMP_ROOT="$runtime/tmp"

python3 -B -m modules.strategy_hermes --state-dir "$runtime/stage1-demo-ledger" replay tests/fixtures/stage1_trade_cycle.json
python3 -B -m modules.strategy_hermes --state-dir "$runtime/stage1-demo-ledger" inspect stage1:demo
python3 -B -m modules.strategy_hermes --state-dir "$runtime/stage1-demo-ledger" report stage1:demo retreat
python3 -B -m modules.strategy_hermes --state-dir "$runtime/stage1-demo-ledger" recover-temp

STAGE1_TEST_ROOT="$runtime/test-tmp/stage1-tests" python3 -B -m unittest discover -s tests -v
```

## 调测实测

- 29 项测试通过。连续状态回归覆盖加强×3、减弱×3、维持×2、重复证据、旧账本恢复和减弱仍只减仓一次。附录 B01–B08、B10–B12 有对应规则或行为测试；B06/B07 通过共享计算层，B09 的手动场景修订和完整重算在 Stage 3。
- CLI 冻结样本：买入 1,000 份，当日减仓等待 T+1，下一交易日卖出 500 份；重复减弱不新增订单，明确退潮后卖出余下 500 份。最终 CLOSED，期末现金 11,500 元，毛利润 1,500 元，毛收益 15.00%。活动轮动链为空，退潮历史与原报告可查。重开数据库仍是 1 个周期、3 笔成交。
- 附录 B06：期末现金 10,950 元、利润 950 元、场景毛收益 9.50%。费用测试覆盖 1,000 分转换为 10 元、现金不足一手以及明确费用配置下的净收益。
- D04/D06/D07/D08/D09 覆盖含否定词报告、坏 ETF 只限制该项、旧报价不覆盖新报价、成交后 SIGKILL 恢复、重复/并发提交和旧周期规则冻结；冲突事件验证整个提交回滚。
- 临时文件覆盖 C01/C02/C03/C04/C06/C08/C09/C10/C11 的离线场景：异常与强杀清理、活动锁、共享预算、越界/链接、回收失败记录、持久证据复读和低空间拒绝。磁盘容量保护是应用预算；HTTP 下载、第三方缓存映射、生产硬配额与 Web 临时目录留后续接入。
- 两个契约文件可读，旧 Hermes v1 schema 与原件字节一致。运行时执行离线入口所需字段及逐项校验，保留合法旧报告；完整供应商协议与真实样本联调待 Stage 2。

## 后续阶段

Stage 2 接既有共享行情、低频映射、五轮 Hermes、受控 MX、真实穿透和 OCTOP，并接 PostgreSQL 正式账本；另做请求预算、Outbox 和消息失败测试。Stage 3 实现手动场景修订、组合重算和有界任务。Stage 4 验收 Web、CI、迁移、生产镜像及手动发布。LR 只保留后续盘后参考边界。
