# MTTR Guard：多 Agent 协同故障处置平台

面向大型互联网公司微服务体系的 SRE 项目。第一阶段聚焦**发布后服务异常的证据收集、止血建议、受控回滚和恢复验证**，减少值班工程师跨监控、调用链、发布平台反复查证的时间。

这是针对“字节这类公司的规模和工程要求”设计的参考项目，不代表字节内部架构或已经部署的系统。当前交付包含可运行的后端核心、故障模拟、可选真实模型适配器、测试和生产试点方案；尚未连接线上系统，也没有真实 MTTR 改善数据。

## 从哪里开始

- [企业落地方案](docs/production-design.md)：场景、架构、职责、接口、容量、安全、试点排期。
- [评估方案](docs/evaluation.md)：MTTR 定义、对照组、故障注入、准入条件。
- [接入契约](docs/integration-contracts.md)：监控、证据、发布执行、审批与工作流。
- `mttr_guard/core.py`：持久化状态机、方案/审批绑定、策略、模拟执行、恢复验证。
- `mttr_guard/agents.py`：三个并行诊断角色、反证审查角色、规则基线与 Ollama 模型适配。
- `mttr_guard/alertmanager.py`：带作用域检查的 Alertmanager 消息标准化。
- `tests/`：关键失效路径测试；不需要在线模型或生产凭据。

## 本地运行

要求 Python 3.11+，核心与测试仅使用标准库。本次在 Windows / Python 3.14.7 验证。

在此 README 所在目录运行：

```powershell
# 生成诊断和待审批方案，默认停在审批处
python -m mttr_guard

# 明确使用模拟审批、模拟执行及虚拟时间，跑完整闭环
python -m mttr_guard --simulate-approval --output artifacts/release-report.json

# 三类不得自动回滚的故障
python -m mttr_guard --scenario downstream
python -m mttr_guard --scenario migration
python -m mttr_guard --scenario missing

# 验证
python -m unittest discover -s tests -v
```

成功场景：`NEW → DIAGNOSING → AWAITING_APPROVAL → APPROVED → VERIFYING → RECOVERED`。

反证、缺失证据或模型不可用：`DIAGNOSING → ESCALATED`。验证超时同样升级人工；原型升级是持久化状态，不会实际发送通知。

默认临时数据库位于 `artifacts/tmp/`，正常退出删除对应数据库文件。加 `--db artifacts/my-demo.db` 可保留状态，首次运行需使用尚未初始化模拟服务的新数据库。每次示例都重新注入同一模拟服务，因此不能重复用同一个已初始化的 DB 执行 demo；实际核心可重新实例化 Engine 读取状态和继续执行。

`--simulate-approval` 会显式创建虚拟审批人，不能用于真实审批。演示中的 90 秒是加速的虚拟时间，**不是实测恢复速度、MTTR 基准或降幅**。

## 使用真实模型

默认 `RuleReasoner` 是确定性规则基线，不调用 LLM。可使用已启动的本地 Ollama 和已安装且支持结构化输出的模型：

```powershell
python -m mttr_guard --model YOUR_INSTALLED_MODEL --ollama-url http://127.0.0.1:11434
```

模型模式会发出四次有独立职责、上下文和结构化结果的模型请求：指标、调用链、变更并行；反证审查随后读取前三者结果。适配器依据 [Ollama 结构化输出接口](https://docs.ollama.com/capabilities/structured-outputs) 实现。模型名由操作者提供，模型服务不会自动下载或启动。

每个角色最多等待 25 秒，HTTP 请求超时为 20 秒；两轮分析的逻辑等待上限约 50 秒。底层同步 HTTP 线程可能在协程超时后继续到网络超时，生产 worker 还需进程级取消和请求预算。单个响应上限 128 KiB，输出必须引用给定证据 ID。异常统一变成 `unknown`，禁止把模型异常当成可执行方案。

当前已验证请求/响应契约与故障处理，未运行真实模型质量评测。此适配器传入的是合成证据；生产证据须经过企业网关脱敏、租户隔离与访问授权。结构化输出和提示词不能单独保证推理正确，确定性策略、人工审批和执行端校验仍然生效。

## 已实现与待落地边界

| 能力 | 当前实现 | 生产化工作 |
|---|---|---|
| 协同分析 | 3 个并行分析角色 + 1 个反证角色 | 内网模型网关、按角色回归评测、成本与容量限制 |
| 告警接入 | Alertmanager payload 解析、作用域验证、单告警事件幂等 | 认证 HTTP receiver、可靠入队、关联聚合、分页/背压 |
| 数据证据 | 有 ID/时间/作用域的模拟指标、调用链、变更 | 真实只读采集器、来源完整性、范围查询、脱敏 |
| 流程持久化 | SQLite 状态与事件；可重建 Engine 继续操作 | Temporal 调度、PostgreSQL、断点恢复与分布式服务锁 |
| 策略和审批 | 固定 Runbook、哈希绑定、5 分钟 TTL、角色/作用域检查 | IAM 认证、审批 UI/回调验签、签名策略与审批撤销 |
| 执行 | 同一 SQLite 事务内的模拟回滚与幂等回执 | 发布平台 CAS、远端幂等键、超时对账、补偿流程 |
| 验证 | 错误率、P99、流量、对照组、版本、连续新样本 | 连续时间窗口、业务成功率、足量请求、持久化定时器 |
| 审计 | 本地事件表 + JSON 导出 | 独立不可改写存储、读取/拒绝审计、权限与保留策略 |

原型中的 `Principal` 是可信进程内身份对象，**不构成外部认证系统**。没有对外 HTTP 服务和生产写入适配器；不能直接把核心方法暴露成无鉴权 API。SQLite 原子性仅覆盖本地模拟副作用，不覆盖真实发布请求。

## 为什么采用这一切入点

发布回退具备明确的变更对象、版本前置条件和可验证结果，适合先在一个团队的灰度服务上验证收益。它仍然要求兼容性检查；存在数据库迁移、下游故障或版本变化时，直接回滚可能无效或危险。

平台遵循先恢复服务、后完善根因分析的处置顺序；这与 [Google SRE 的故障响应实践](https://sre.google/workbook/incident-response/) 一致。多 Agent 是否值得引入，则必须通过本项目的对照实验决定。
