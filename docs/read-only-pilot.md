# v0.3：真实取证、人工处置与恢复验证

本页保留轻量演示与证据接入契约。需要独立进程监督、OIDC、审计和备份恢复时，使用 [v0.4 持续试点入口](production-pilot.md)。

目标是减少查证、寻找入口、核对建议和持续观察的时间。新增持续运行的 HTTP 服务、值班工作台、Prometheus 只读采集、证据网关契约、持久化任务、人工审核记录和恢复定时观察。实际发布仍由人员在原平台完成。当前是单主机试点，没有真实线上 MTTR 改善数据。

## 本地体验

要求 Python 3.11+，无第三方 Python 依赖。在仓库根目录运行：

```powershell
python -m mttr_guard.pilot demo
```

打开 `http://127.0.0.1:8787`，把启动输出指定的 `artifacts/pilot-demo/operator.token` 文件内容粘贴进工作台。令牌仅保留在网页内存，不写入 URL 或浏览器持久存储。

1. 选择自动生成的结算故障，点击“我来接手”，查看证据、版本和 Runbook。
2. 填写核对依据，点击“接受建议（不执行）”。这只记录人工判断。
3. 在另一终端运行 `python -m mttr_guard.pilot demo-recover`，让**演练发布端**回到 r41。
4. 在工作台填写操作说明和目标版本 r41，点击“记录已人工处置，开始验证”。
5. 等待三个十秒窗口。这里是真实等待时间，但所有数据仍为 DEMO，不纳入真实 TTR。

Ctrl+C 停止；数据库保留。再次启动 demo 创建新演练事件。多实例须指定不同 `--directory`、`--port`、`--evidence-port`；demo-recover 使用对应目录和证据端口。真实运行用 serve，它不会制造告警。

完整无浏览器集成演练：`python -m scripts.run_pilot_drill`。使用临时数据库/端口，经过认证 HTTP 接收、HTTP 取证、人工核对记录和三个真实时间窗口，中途重建 worker；报告在 `artifacts/pilot-http-drill.json`。不等同于宿主机断电测试。

## 接入真实测试服务

复制 `config/pilot.example.json` 为 `config/pilot.local.json`，修改五元作用域、Owner、平台链接、已审核 Runbook、Prometheus URL/查询、证据网关 URL 和恢复策略。

```powershell
python -m mttr_guard.pilot serve --config config/pilot.local.json --db artifacts/pilot.db
```

身份凭据由环境变量提供，至少 32 字符，不写入 JSON 或 Git。示例需要 `MTTR_RECEIVER_TOKEN`、`MTTR_OPERATOR_TOKEN`、`MTTR_VIEWER_TOKEN`；三个值必须不同。数据源使用分别配置的只读凭据 `MTTR_PROMETHEUS_TOKEN`、`MTTR_EVIDENCE_TOKEN`。无需凭据的数据源可删除对应 token_env 配置。

服务只绑定 127.0.0.1。跨主机接 Alertmanager 时使用同主机企业内网 TLS 反向代理，限制来源、请求速率和连接数。不要把标准库 HTTP 服务直接当成公网或高可用入口。令牌撤销或配置变化需重启，尚未接企业 OIDC/IAM 和在线轮换。

合并 `config/alertmanager.example.yml` 到现有配置，保留现有 paging。告警必须有完整 tenant/environment/cluster/namespace/service 标签，与 token 的授权服务匹配。本项目不会发送 Slack、飞书或短信，也不替代原告警通知。

协议依据：[Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)、[Alertmanager 配置](https://prometheus.io/docs/alerting/latest/configuration/)。

## 指标要求

使用 GET /api/v1/query，同批固定 evaluation time。查询由可信配置固定，模型、告警和浏览器不能提供查询或端点。

- error_rate、control_error_rate：灰度/稳定组错误率，0–1。
- p99_ms：灰度延迟，毫秒；qps：灰度请求速率，与 baseline_qps 同口径。
- requests、control_requests：同一窗口内灰度/稳定组请求数。
- sample_age_seconds：底层时间序列最大样本年龄，避免查询时间戳新鲜但原始数据过旧。

拒绝 NaN/Infinity、空或多序列、过期查询、partial warnings、过旧原始样本以及超过 1 MiB 的响应。HTTP socket 超时 5 秒、查询超时参数 4 秒，不等于严格的流式响应总截止时间。禁用继承桌面代理和 HTTP 重定向，避免凭据跟随跳转。

配置是信任边界：程序不解析 PromQL 证明租户隔离。配置者必须核查全部选择器、指标单位、样本口径，以及查询窗口与 policy.window_seconds 一致。示例指标和标签需要按实际系统修改；回退后灰度/稳定标签仍须可查询。采集权限应由源系统进一步限制。sample_age_seconds 不能发现所有完全消失的实例，仍需现有遥测完整性告警。

## 变更和调用链网关

需对公司现有系统提供一个只读 JSON 转换端点。**未内置 Jaeger、Tempo、Kubernetes 或企业发布平台的原生适配器。** 返回契约：

```json
{
  "mode": "READ_ONLY",
  "scope": {"tenant": "commerce", "environment": "staging", "cluster": "cn-east-1", "namespace": "trade", "service": "checkout"},
  "observed_at": 1800000000.0,
  "window_seconds": 60,
  "deployment": {
    "revision": "r42", "previous_revision": "r41", "changed_at": 1799999820.0,
    "traffic_percent": 5, "known_good": true, "schema_migration": false, "rollout_complete": true
  },
  "traces": {"downstream_healthy": true, "failing_span": "checkout.validate", "sample_count": 200}
}
```

时间是 Unix 秒，示例值要换成真实数据。observed_at 应反映包中**最旧源证据时间**，不得只填接口响应时间掩盖旧数据。允许网关最多 1 秒钟差，超过配置的新鲜度即失败。缺失信息不能填 true；网关负责来源真实性、脱敏、授权及采样质量。未使用的额外字段不会落入证据包。

发布关联使用 detected_at - changed_at，避免把故障之后的发布误当成最初原因。上述条件只支持止血候选，不证明根因。

## 人工流程与接口

状态：QUEUED → RECOMMENDED / NEEDS_ATTENTION → OBSERVING → RECOVERED / NEEDS_ATTENTION。

- POST /v1/webhooks/alertmanager：receiver。整批事件和任务同事务持久化后返回 202；scope + fingerprint + startsAt 去重；resolved 不确认恢复。
- GET /api/session、/api/incidents、/api/incidents/{id}、/api/metrics：viewer/operator，仅本服务。事件列表每页最多 100 项，可传 before=next_cursor；详情含最近 200 条事件。
- POST /api/incidents/{id}/ack：接手。
- POST /api/incidents/{id}/review：plan_hash、accept/reject decision、reason。
- POST /api/incidents/{id}/refresh：reason，重新取证，保留首次时间。
- POST /api/incidents/{id}/observe：reason、target_revision，记录已人工处置并开始观察。
- GET /healthz 检查存储；GET /readyz 额外检查 worker 150 秒内有心跳。

所有 operator POST 要求 request_id；相同输入重放返回现状，同 ID 不同输入拒绝。身份来自 Bearer token，不信任请求中的 subject/role。receiver 不能读事件，viewer 不能写。

“接受建议”记录评价，**不是生产执行授权**。服务没有线上发布写接口。人员可在原平台选择不同处置，但须报告说明和预期版本。人工记录不会直接把故障置为恢复。

恢复使用事件接收时冻结的策略。默认三个不重叠窗口，检查错误率、P99、流量、样本量、下游、版本及 rollout 完成。缺证据、不健康或缺窗重置连续计数；超时转人工。不会以低流量、版本不符或告警 resolved 宣称恢复。

## 持久化、容量与边界

SQLite inbox/jobs 与事件同库。worker 获取 120 秒租约，网络操作在事务外，完成写入时校验租约；旧 worker 无法覆盖接管结果。崩溃后任务可再领取，重复只读取证无发布副作用。诊断采集失败按 5、10 秒退避，第三次失败转人工；观察失败持续到截止时间。

scope、query、端点、mode、基线和策略摘要绑定事件。实质配置变化不能静默改变已有事件口径；需完成旧事件或保留其旧配置实例，目前无配置迁移界面。

未完成任务上限 1000，满时整批回滚并返回 503，依赖发送方重试。请求上限 1 MiB，最多 16 个 HTTP 并发连接，默认 2 个后台 worker、最多 4 个。它们是边界，**不是压测容量承诺**。适合少量服务的单机试点；多主机高可用、企业网关、数据保留、完整拒绝审计、不可改写审计、跨告警关联、遥测完整性和业务成功率仍需补齐。具有数据库权限的人可以修改审计记录。

## 衡量收益

记录首次告警、接收、证据形成、建议形成、接手、审核、人工处置报告和验证恢复。TTR = 验证恢复 - 首次告警，保留人工等待时间。

统计包含真实事件数、未恢复数、已恢复事件 TTR 均值/P50/P90、阶段耗时和建议接受/拒绝；DEMO 单独排除，缺数据返回 null。已完成 TTR 存在幸存者偏差，不能丢弃 unresolved 后宣称整体改善。人工动作时间采用报告接收时间，可能晚于实际操作。

同服务、同故障类型/严重度、同恢复标准下，依次比较原人工工具链、规则工作流、单模型、四角色。优先验证规则取证和工作台节省的查证时间，再评估多模型的增量收益。可用 Ollama，但真实模型建议质量尚未评估。完整方法见 evaluation.md；工程测试不能替代线上 MTTR 对照实验。
