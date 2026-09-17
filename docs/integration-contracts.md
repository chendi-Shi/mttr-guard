# 生产接入契约与实现路线

本文定义完整生产系统的目标契约。v0.3 已实现其中的只读 HTTP 试点：认证告警接收、原子排队、Prometheus/证据网关采集、人工审核记录和持久化恢复观察，实际接口与边界见 [只读试点](read-only-pilot.md)。下文涉及线上执行审批、企业 IAM、跨区域协调等仍是目标设计。v0.2 的 `dispatch/reconcile/revoke_approval` 为进程内协议及独立发布模拟器，详见 [执行可靠性](execution-reliability.md)。

## 1. 告警平台 → Receiver

建议入口：`POST /v1/webhooks/alertmanager`，企业网关验身份、速率与请求大小，Receiver 完成作用域映射后持久化并可靠入队。租户取自认证服务账户，不允许请求体单方面决定租户。消息体遵循 [Alertmanager webhook 配置与格式](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)。

原型要求每条告警包含 `tenant、environment、cluster、namespace、service` 标签，与可信接入作用域完全匹配。实际公司标签不同则由接入层做显式映射；缺失标签不得默认为生产环境或全局作用域。

示例：

```json
{
  "version": "4",
  "status": "firing",
  "alerts": [{
    "status": "firing",
    "labels": {
      "tenant": "commerce", "environment": "staging",
      "cluster": "cn-east-1", "namespace": "trade", "service": "checkout",
      "alertname": "CanaryErrorRateHigh"
    },
    "startsAt": "2026-09-17T02:00:00Z",
    "fingerprint": "checkout-canary-error-rate"
  }]
}
```

接收成功后返回已持久化的 receipt；原始事件写入和 outbox 写入同事务，避免先应答后丢事件。下游按事件键幂等。原型逐条入库，虽先验证整批但没有批次事务/outbox；异常后发送方重试由去重处理。

单告警幂等键为 `scope + fingerprint + startsAt`。事件聚合是独立逻辑，生产按依赖关系、故障传播与发布时间合并，并保留父子告警。不能把时间接近的所有告警合为一件事故，也不能把同一服务后续独立故障吞掉。

## 2. 工作流 → 证据服务

建议接口：`collect_evidence(scope, incident_window, deployment_revision, query_template_id, deadline)`。

返回字段：`evidence_id、scope、source、query_template_id、query_params、window_start、window_end、collected_at、valid_until、cohort、revision、sample_count、truncated、data、content_hash`。

指标连接器要求按灰度/稳定版本读取错误分子、请求分母、延迟分位数或可正确聚合的 histogram。调用链连接器返回失败 span、下游耗时和采样信息。发布连接器返回当前版本、上一稳定版本、变更时间、灰度比例、进行中变更和迁移兼容信息。

每个连接器使用独立只读凭据；服务端拼装受限查询，模型只能选择查询模板和允许的参数。日志注入的 URL、SQL、PromQL 或命令均不能直接成为工具调用。超时、截断和无数据是不同状态，必须返回明确标记。

原型 `seed_simulator` 一次性生成一致的快照，`simulate_observation` 生成后续指标。替换时不能将生产数据塞入模拟执行器并视为已经上线；必须连同 IAM、工作流与发布端契约一起实现。

## 3. 审批平台 → 工作流

建议接口：`POST /v1/incidents/{id}/approvals`。

请求仅包含 `plan_hash、decision、reason、client_request_id`；`approver_id、roles、authorized_scope` 来自企业身份验证。审批回调使用签名、时间戳、nonce、审批单 ID 和计划哈希，重复回调返回原处理结果。

同意/拒绝/撤销均写事件；执行前查询最终有效审批。超过 TTL 或服务新版本出现时，原审批失效。界面至少展示作用域、当前/目标版本、影响范围、证据来源、Runbook 版本、回退限制和验证条件。执行器不得把“同意排查”解读为“同意回滚”。

原型 `approve()` 接受可信的 `Principal`，只有同作用域 approver 能审批准确 plan hash；`revoke_approval()` 支持执行接纳前撤销，但没有回调验签、组织目录或 Web UI。

## 4. 执行器 → 企业发布平台

```json
{
  "operation_id": "sha256(incident_id + plan_hash)",
  "scope": {
    "tenant": "commerce", "environment": "staging",
    "cluster": "cn-east-1", "namespace": "trade", "service": "checkout"
  },
  "action": "rollback_canary",
  "expected_revision": "r42",
  "target_revision": "r41",
  "max_traffic_percent": 5,
  "fencing_token": 173,
  "approval_reference": "verified-approval-id",
  "deadline": "2026-09-17T02:10:00Z"
}
```

服务端必须在接受动作时原子校验版本和 fencing token，而非客户端先 GET 后不带条件 POST。成功返回持久化的 `operation_id、status、accepted_at、observed_revision`，并提供 `GET operation_id` 对账。重复相同 operation 返回原结果，相同 key 不同请求体拒绝。

生产状态为 `APPROVED → EXECUTING → VERIFYING`，歧义响应转 `EXECUTION_UNKNOWN`，失败转 `ESCALATED`。外部执行慢或连接中断时，工作流保留事实和原操作 ID，不新建动作绕过限制。

不能把 shell 的 exit code=0 当作业务恢复，也不能把“API 已接受发布”当作“rollout 已完成”。执行成功只表示开始进入独立验证。

## 5. 恢复验证 → 故障平台

验证器定时采集**动作之后**的窗口，要求数据新鲜、时间不倒退、有足够流量且没有缺窗。门槛由服务 Owner 配置并作为方案的一部分审批，不能由模型在执行后为达成恢复而下调。

确认后写入 `verified_recovery_at`、条件版本和全部窗口证据；复盘状态独立于恢复状态。若人工确认或覆盖自动判断，记录人工依据和实际恢复时间，不能修改原始事件以美化指标。

## 6. 分步替换原型

1. 保持当前确定性基线，先替换真实只读证据服务，并完成标签/权限/时间窗校验。
2. 接内网模型网关，记录角色级输出与延迟；使用历史事故数据比较模型与规则，保持建议模式。
3. 将进程内调用迁移为持久化工作流 Activities，把模拟身份换成企业 IAM。
4. 接审批 UI/回调和独立审计，完成过期、撤销、重放与权限变更测试。
5. 对接发布平台 CAS、幂等与操作查询；先用 staging 故障注入覆盖执行成功/失败/未知。
6. 接独立真实业务指标和持久化验证定时器，再开展受限试点。生产 Runbook 的授权由业务/SRE Owner 管理。

验收时检查每条链路实际行为和记录，不以“已接 SDK”“已有 Agent 名称”或“能跑一次 demo”作为生产完成标准。
