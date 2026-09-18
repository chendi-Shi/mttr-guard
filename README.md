# MTTR Guard：多 Agent 协同故障处置平台

面向大型互联网公司微服务体系的 SRE 项目。第一阶段聚焦**发布后服务异常的证据收集、止血建议、受控回滚和恢复验证**，减少值班工程师跨监控、调用链、发布平台反复查证的时间。

这是面向大型微服务团队故障处置需求的参考项目，不代表字节内部架构或已经部署的系统。当前包含 v0.2 受控执行模拟，以及 v0.3 可持续运行的只读试点：告警 HTTP 接入、值班工作台、Prometheus 取证、持久化任务、人工处置记录、定时恢复验证和耗时统计。尚未接入用户线上系统，也没有真实 MTTR 改善数据。

## 先体验值班工作台（v0.3）

```powershell
python -m mttr_guard.pilot demo
```

打开 `http://127.0.0.1:8787`，把本地 `artifacts/pilot-demo/operator.token` 文件中的令牌粘贴进工作台。查看自动生成的事件、证据和回退建议。在另一终端运行 `python -m mttr_guard.pilot demo-recover` 后，在页面记录已人工处置和目标版本 `r41`，等待三个十秒窗口验证恢复。全部是合成数据，排除在真实 TTR 统计之外。

真实测试环境使用 `python -m mttr_guard.pilot serve --config config/pilot.local.json --db artifacts/pilot.db`。配置样例为 `config/pilot.example.json`，需要现有 Prometheus 和只读变更/调用链证据网关；平台不执行真实发布。详见 [只读试点接入手册](docs/read-only-pilot.md)。

## 持续试点入口（v0.4）

新增独立 API/诊断/观察进程、70 秒任务进程期限、OIDC API 令牌验证、权限热撤销、人工暂停/交接、HMAC 审计、运行指标及可校验的备份恢复。它是**单机加固的只读试点**：仍使用 SQLite，没有线上 MTTR 改善证据或多机高可用保证。部署配置、身份约束和操作命令见 [v0.4 运行手册](docs/production-pilot.md)。

安装 `requirements-enterprise.txt` 后，用 `python -m scripts.run_enterprise_drill` 可独立复现 API、worker 重启与备份恢复演练，过程不会操作真实发布平台。本地验证记录见 [验证报告](docs/validation.md)。旧的 `pilot demo` 保留作快速体验。

**v0.4.1 安全修复**：修复“数据库状态被篡改后，正常接手操作将其重新签入审计链”的漏洞。事务读取或写入之前校验完整审计链及受保护状态；失败后 API 返回 503，worker 停止处理并等待人工恢复。已有完好 v0.4 审计库可以直接读取，已经损坏的库拒绝启动。详见 [审计故障处置](docs/audit-integrity.md)。

**v0.4.2 校验性能改进**：每个事务仍扫描全链和受保护状态，仅复用与进程内已验证原文及字段类型完全一致的记录校验结果；新增、修改或未缓存的记录重新校验。默认缓存预算每进程 16 MiB，支持关闭。本机 1 万条历史记录的合成基准中，查询加访问审计的 P50 从 438 ms 降至 97 ms；这是缓存热态的审计开销，不能当作生产容量或 MTTR 改善。实现、冷启动和预算耗尽的边界见 [校验缓存说明](docs/audit-cache.md)。

## 真实环境验收（v0.5）

新增可运行的证据源预检、审计结果导出、固定方案的事件对照评估及限速 HTTP 部署探测。评估保留未恢复/缺失事件，校验输入哈希和检测起点，按预定分层权重报告有限观察期内的结果与探索性区间；数据不足、缺少安全复核或出现安全事件时不会报通过。模拟数据、影子分析和声明的生产数据都不会自动得到“生产就绪/已降低 MTTR”的认证。完整接入命令、数据格式和验收边界见 [真实接入与效果验收](docs/real-world-validation.md)。

当前没有真实环境访问权或真实事故数据，尚未执行生产容量或线上效果验证。代码提供了收集和分析这些证据的入口，不能替代实际试点。

## 从哪里开始

- [v0.2 执行可靠性](docs/execution-reliability.md)：跨系统对账、服务锁、审批撤销与进程崩溃演练。
- [v0.3 只读试点](docs/read-only-pilot.md)：运行工作台、接真实证据、人工接管、持久化任务和效果测量。
- [v0.4 持续运行](docs/production-pilot.md)：分离进程、身份校验、审计、备份恢复、部署及剩余边界。
- [企业落地方案](docs/production-design.md)：场景、架构、职责、接口、容量、安全、试点排期。
- [评估方案](docs/evaluation.md)：MTTR 定义、对照组、故障注入、准入条件。
- [v0.5 验收工具](docs/real-world-validation.md)：实际证据源预检、可审计导出、完整事件评估和限速部署探测。
- [接入契约](docs/integration-contracts.md)：监控、证据、发布执行、审批与工作流。
- `mttr_guard/core.py`：持久化状态机、方案/审批绑定、策略、模拟执行、恢复验证。
- `mttr_guard/agents.py`：三个并行诊断角色、反证审查角色、规则基线与 Ollama 模型适配。
- `mttr_guard/alertmanager.py`：带作用域检查的 Alertmanager 消息标准化。
- `mttr_guard/execution.py`：持久化执行意图、单次派发、结果未知处理及只读对账。
- `mttr_guard/deployment_simulator.py`：独立数据库的发布平台模拟器，支持服务端幂等、版本前置条件与故障注入。
- `tests/`：关键失效路径测试；不需要在线模型或生产凭据。

## 本地运行

要求 Python 3.11+，核心与 v0.3 使用标准库；v0.4 API 及其测试需额外安装 `requirements-enterprise.txt`，未安装时相关测试会跳过。v0.2 原始验证使用 Python 3.14.7，v0.3/v0.4 本地验证使用 Windows / Python 3.12.14。以下命令演示 v0.2 模拟执行路径：

在此 README 所在目录运行：

```powershell
# 生成诊断和待审批方案，默认停在审批处
python -m mttr_guard

# 明确使用模拟审批、模拟执行及虚拟时间，跑完整闭环
python -m mttr_guard --simulate-approval --output artifacts/release-report.json

# 发布端已完成但响应丢失：重启后按原操作 ID 对账，不重复执行
python -m mttr_guard --simulate-approval --fault after_submit

# 请求未送达：保留结果未知与服务锁，不盲目重发
python -m mttr_guard --simulate-approval --fault before_submit

# 三类不得自动回滚的故障
python -m mttr_guard --scenario downstream
python -m mttr_guard --scenario migration
python -m mttr_guard --scenario missing

# 验证
python -m unittest discover -s tests -v
```

默认适配器执行场景：`NEW → DIAGNOSING → AWAITING_APPROVAL → APPROVED → EXECUTING → VERIFYING → RECOVERED`。

遇到执行超时或不可信回执时进入 `EXECUTION_UNKNOWN`，按原操作 ID 查询。确认成功后才能验证恢复；查不到时不自动重发。执行接纳前允许撤销审批，进入 `CANCELLED`。

反证、缺失证据或模型不可用：`DIAGNOSING → ESCALATED`。验证超时同样升级人工；原型升级是持久化状态，不会实际发送通知。

默认临时数据库位于 `artifacts/tmp/`，本地事件库与独立发布模拟库正常退出后均清理。加 `--db artifacts/my-demo.db` 可保留两份状态，首次运行需使用尚未初始化模拟服务的新数据库。每次示例都重新注入同一模拟服务，因此不能重复用同一个已初始化的 DB 执行 demo；实际核心可重新实例化 Engine 读取状态并调用 `reconcile()`。`--execution-mode atomic` 可保留最初的单事务模拟方式。

`--simulate-approval` 会显式创建虚拟审批人，不能用于真实审批。演示中的 90 秒是加速的虚拟时间，**不是实测恢复速度、MTTR 基准或降幅**。

## 使用真实模型

默认 `RuleReasoner` 是确定性规则基线，不调用 LLM。可使用已启动的本地 Ollama 和已安装且支持结构化输出的模型：

```powershell
python -m mttr_guard --model YOUR_INSTALLED_MODEL --ollama-url http://127.0.0.1:11434
```

模型模式会发出四次有独立职责、上下文和结构化结果的模型请求：指标、调用链、变更并行；反证审查随后读取前三者结果。适配器依据 [Ollama 结构化输出接口](https://docs.ollama.com/capabilities/structured-outputs) 实现。模型名由操作者提供，模型服务不会自动下载或启动。

每个角色最多等待 25 秒，HTTP 请求超时为 20 秒；两轮分析的逻辑等待上限约 50 秒。底层同步 HTTP 线程可能在协程超时后继续到网络超时；v0.4 worker 额外以 70 秒总期限终止整个取证/分析子进程。单个响应上限 128 KiB，输出必须引用给定证据 ID。异常统一变成 `unknown`，禁止把模型异常当成可执行方案。

当前已验证请求/响应契约与故障处理，未运行真实模型质量评测。此适配器传入的是合成证据；生产证据须经过企业网关脱敏、租户隔离与访问授权。结构化输出和提示词不能单独保证推理正确，确定性策略、人工审批和执行端校验仍然生效。

## 已实现与待落地边界

下表保留 v0.2/v0.3 主流程范围；v0.4 已补本地 HMAC 审计、读取/拒绝审计与 OIDC access token 校验，具体威胁边界见运行手册。独立不可改写审计、真实 IAM 联调及多机调度仍未完成。

| 能力 | 当前实现 | 生产化工作 |
|---|---|---|
| 协同分析 | 3 个并行分析角色 + 1 个反证角色 | 内网模型网关、按角色回归评测、成本与容量限制 |
| 告警接入 | Bearer 作用域认证、Alertmanager HTTP、原子入库排队、去重、分页、队列上限 | 企业 IAM、关联聚合、压测与高可用接入 |
| 数据证据 | Prometheus HTTP 只读采集、变更/调用链 JSON 网关契约、来源/范围/新鲜度检查；保留模拟器 | 企业网关原生适配、采集完整性、范围查询、脱敏 |
| 流程持久化 | SQLite 状态与事件；执行意图、重启对账、同数据库服务锁 | Temporal 调度、PostgreSQL、跨区域租约/fencing、人工解锁流程 |
| 策略和审批 | 固定 Runbook、哈希绑定、TTL、作用域检查、执行接纳前撤销 | IAM 认证、审批 UI/回调验签、签名策略 |
| 执行 | 独立发布模拟库的幂等/CAS、未知结果处理、只读对账；保留旧模拟路径 | 真实发布平台适配器、远端取消、补偿与接管流程 |
| 验证 | v0.3 持久化任务按不重叠窗口采集，检查错误率/P99/流量/样本数/下游/版本，缺窗重置 | 企业业务成功率、遥测完整性、生产 SLO 校准 |
| 审计 | 本地事件表 + JSON 导出 | 独立不可改写存储、读取/拒绝审计、权限与保留策略 |

v0.2 中的 `Principal` 是可信进程内身份对象，**不构成外部认证系统**。v0.3 HTTP 入口独立验证绑定服务与角色的 Bearer token，只绑定回环地址；仍需企业 TLS 入口和 IAM 集成。没有生产写入适配器，不能直接把核心方法暴露成无鉴权 API。执行协议的验证对象仍然是独立 SQLite 模拟平台。未知结果的服务锁不会自动过期或释放。

## 自动验证

GitHub Actions 配置在 Linux/Windows、Python 3.11/3.14 上执行测试、两条执行故障演练及 v0.3 HTTP 试点演练，并检查输出状态。流水线只读仓库、不使用部署凭据，官方 Actions 固定到提交版本。最新执行结果见 [Actions](https://github.com/chendi-Shi/mttr-guard/actions)；本地未推送的修改不代表远程已通过。

## 为什么采用这一切入点

发布回退具备明确的变更对象、版本前置条件和可验证结果，适合先在一个团队的灰度服务上验证收益。它仍然要求兼容性检查；存在数据库迁移、下游故障或版本变化时，直接回滚可能无效或危险。

平台遵循先恢复服务、后完善根因分析的处置顺序；这与 [Google SRE 的故障响应实践](https://sre.google/workbook/incident-response/) 一致。多 Agent 是否值得引入，则必须通过本项目的对照实验决定。
