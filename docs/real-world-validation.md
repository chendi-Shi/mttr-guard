# v0.5：真实接入与效果验收工具

本版本把此前文档中的部分验收步骤变成了可运行工具。**仓库尚未取得真实环境访问权或真实事故数据，所以当前没有生产容量、真实 MTTR 降幅或大规模上线资格的结论。** 工具可以作用于指定真实部署和历史数据，但数据来源、研究设计与实际运行仍需业务方完成。声明 `PRODUCTION` 不会让输入自动成为可信生产数据。

## 1. 先建立可复查的试点档案

选择一个明确服务、五维作用域和故障类型，记录负责人、发布版本、脱敏配置、机器规格、数据量、负载模型、SLO 和变更单。将这些材料与输入/输出报告保存在团队现有受控档案中；本项目没有自动对接审批或外部证据仓库。

第一阶段只读取证和建议。先接 staging，再按团队准入流程接真实服务；不得把模拟发布执行器接到线上。现有 SQLite 实现仍是单机，下面的工具不会补出主机故障切换或跨区域恢复能力。

## 2. 检查实际证据源

配置入口见 [运行手册](production-pilot.md)，包括真实 Prometheus 查询、变更/调用链只读网关及凭据环境变量。执行：

```powershell
python -m mttr_guard.validation preflight --config config/enterprise.local.json --service checkout --output artifacts/preflight-run-001.json
```

命令使用独立采集子进程，20 秒总期限，仅执行配置中的只读查询。它拒绝 DEMO 服务，校验作用域、证据模式、时间窗、新鲜度和数据格式，报告请求样本量、数值指标、恢复策略、配置哈希及证据摘要。没有数据或流量不足时不能报通过。

`source_contract_passed` 只说明这次配置和返回数据满足采集契约。它不能认证源数据一定来自生产，也不证明 IdP、TLS 网关、告警路由、通知、人工接管或持续观测都已验收。核对 PromQL 标签和真实服务的对应关系，保留独立指标截图/查询记录或源系统归档，并做真实 IdP 联调。

## 3. 在目标部署执行有上限的负载探测

复制 `config/load-probe.example.json` 为被 Git 忽略的 `config/load-probe.local.json`，填写实际 HTTPS 地址和目标服务，依据自己的 SLO 修改门槛。示例门槛是占位配置，不是平台承诺。通过既有凭据管理方式向环境注入 `MTTR_LOAD_TOKEN`（对应 viewer/operator），不要将令牌放入命令行或计划文件。

```powershell
python -m mttr_guard.load_probe --plan config/load-probe.local.json --token-env MTTR_LOAD_TOKEN --output artifacts/load-run-001.json
```

当前工作负载明确限定为 `GET /api/operations` 及服务端访问审计，**不等同于告警接收、并行诊断或模型容量**。按预定时间发起请求，不因服务变慢而自动降低给定负载；并发额度满、调度跟不上、超时、429、错误返回、未就绪和非法响应都记入失败分母。延迟从预定发送时刻开始计算；没有启动的请求不进入延迟分位数，但会导致错误率门槛失败。报告包含计划哈希、实际请求配置、P50/P95/P99、成功吞吐、错误/跳过计数和状态码。

边界：每秒 1–100 次、1–120 秒、1–32 并发，每请求 0.1–10 秒总期限，响应上限 64 KiB。远程地址必须使用 HTTPS，不跟随重定向、不继承环境代理；明文 HTTP 仅限 localhost/回环。原 API 每来源地址 240 次/分钟的限流仍生效，探测不会绕过它。探测虽然不修改发布平台，但每次请求会产生审计写入，应安排在团队批准的测试窗口。

一次通过只代表该部署、数据量、该段时间和这个工作负载。需要分别覆盖预期审计历史规模、峰值/突发告警、真实取证耗时、模型期限、人工操作、长时间稳定性、磁盘/进程故障与恢复。CLI 没有自动发现最大容量，也没有产生生产 QPS 认证；部署档案和工作负载代表性必须由服务负责人复核。

## 4. 固定方案、完整名册和结果

评估输入为三个 JSON，版本均为 `schema_version: 1`。使用下面的生成器可查看完整结构，它只生成明确标记的 **SYNTHETIC** 示例：

```powershell
python -m scripts.make_study_fixture --output-directory artifacts/study-example
```

真实研究应在收集结果前，将计划登记到独立的变更/实验系统，取得不可随结果改动的版本引用，并另建实际数据文件。不要把示例的 `domain` 改名后作为真实结果提交。

| 文件 | 关键内容 |
| --- | --- |
| plan.json | study_id、domain（PRODUCTION/STAGING/SYNTHETIC）、exposure（INTERVENTION/SHADOW）、registration_ref、inclusion_rule、registered_at、window_start/end、analysis_cutoff、horizon_seconds、baseline/candidate、minimum_per_arm_per_stratum、minimum_improvement_seconds、strata 的固定权重、bootstrap_samples、seed |
| registry.json | plan_sha256、独立完整名册的 source_ref、全部合格事件 incidents；每条 id、domain、arm、stratum、detected_at、source_ref |
| outcomes.json | registry_sha256、已取得的结果 incidents；每条 id、last_observed_at、recovered_at（可为 null）、recovery_evidence_ref、source_ref、safety_events（未知则 null）、safety_review_ref |

名册的统计单位应是去重、合并后的事故，而不是同一故障产生的多条告警。登记时固定服务、严重度和故障类型等分层及权重，并检查 on-call、时段、审批策略等混杂因素。两组不能因为事件难度构成不同而随意合并比较。baseline/candidate 可以分别对应人工、规则、单 Agent 或多 Agent，但需要公平的工具、预算和恢复判据。

`plan_sha256` 和 `registry_sha256` 使用 `mttr_guard.core.digest` 对完整 JSON 对象规范化后计算，生成器展示了计算方式。哈希用于把报告绑定到确定输入，不是签名，不证明来源真实、名册完整或登记时间真实。将计划、名册、源系统记录和报告一起交给独立评审者复核。已知 `synthetic://` 来源或 DEMO 来源不能进入真实域研究。

## 5. 从运行库导出审计可追溯的结果

提前在名册登记本系统事件 ID 及原始合格检测时间；基线可以从独立事故平台按相同契约导入。`scope.local.json` 是包含 tenant/environment/cluster/namespace/service 的对象。提供原数据库审计密钥后执行：

```powershell
python -m mttr_guard.validation export --db artifacts/enterprise.db --scope config/scope.local.json --registry artifacts/study/registry.json --output artifacts/study/export-001.json
```

导出使用只读 SQLite 事务，在同一快照中完整验签再读取指定作用域。拒绝无密钥审计、损坏数据、注册检测时间与数据库不一致的记录；排除 DEMO。输出绑定名册哈希和审计链头，记录未在名册内的同作用域事件数量，便于核对选择偏差；不导出摘要、令牌或完整调用链。

未恢复事件的随访只到最后实际记录的入库/观察时间，不假设持续观察到了导出时刻。恢复时间来自系统验证记录；独立业务证据仍需复核。安全事件数保留 null，不能自动填 0。独立评审后提供安全事件计数与评审引用，再将其作为 outcomes 输入。审计链只能说明本地历史一致，并不能认证上游观测源或独立复盘真实性。

## 6. 计算结果并处理不完整观测

```powershell
python -m mttr_guard.validation evaluate --plan artifacts/study/plan.json --registry artifacts/study/registry.json --outcomes artifacts/study/outcomes.json --output artifacts/study/report-001.json
```

主指标是预先固定观察上限 τ 内的平均 `min(TTR, τ)`，属于限制平均恢复时间，不是无限观察下的 MTTR。定义对应 [RMST 方法论文](https://pmc.ncbi.nlm.nih.gov/articles/PMC3922847/)。本实现采用更保守的完整观察要求，不对早期失访拟合生存曲线。

- 已验证恢复：使用恢复时长，上限截到 τ；完整观察到 τ 仍未恢复：取 τ，不能删掉。
- 未恢复且随访早于 τ：只知道值在“已观察时长到 τ”之间；缺失结果则给出 0 到 τ 的范围。它们保留在分母中，阻止给出组间点比较。
- 仅对全部事件有完整上限内结果、各层样本达到预登记要求的情况，给出按固定权重计算的差异和组内分层 bootstrap 探索性区间。这里不是 Kaplan–Meier 外推；缺失观测不会通过插值变为恢复。
- 主指标和完成事件的均值/P50/P90 分开报告，后者不能作为删除失败样本后计算“改善”的依据。
- 时间没有时区、未来截止时间、事后登记时间、重复/未登记结果、域混入、缺少恢复证据引用或输入哈希不一致时拒绝计算。

`numeric_gate_met` 要求区间下界严格大于预先约定的绝对改善秒数，且无缺失安全复核、候选组无报告的安全事件。`insufficient_evidence` 表示样本/随访/复核不足；`safety_gate_failed` 表示候选组出现报告的安全事件；`numeric_gate_not_met` 表示完整数据未达到数值要求。

数值门槛不能证明因果。bootstrap 假定层内事故可作为独立观察单位；相关事故、反复复用场景、操作者学习效应和非随机分配会破坏解释。最低样本数不是统计功效计算，零次安全事件也不证明风险为零。影子分析无法证明实际缩短了线上恢复时间。所有报告都保留来源真实性、名册完整性、生产准入和因果结论的未验证状态，后续须结合预先设计的受控试点与独立复核形成结论，不能仅修改一个配置标志解除。

CLI 退出码：0 为该次数据数值门槛/契约检查通过或导出成功；2 为有效报告但证据不足/门槛不满足；1 为输入或运行错误。退出码 0 不是生产批准。报告原子生成且拒绝覆盖旧文件，重复运行使用新路径。真实输入和报告放在受控存储或 Git 忽略的 `artifacts/`、`config/*.local.json`，不要提交敏感事故数据。

## 当前验证结果的边界

`scripts.run_validation_drill` 通过真实 CLI 验证完整合成数据、缺失结果和被改动的输入。三进程演练增加了本地 HTTP 探测，只是功能检查，不能作为容量测量。新增测试覆盖未恢复事件翻转“成功”结论、固定分层权重、早期失访、数据泄漏/篡改、安全复核缺失、流量过载、超时、重定向和凭据输出边界。详见 [验证记录](validation.md)。

要产生真实结论，仍需：实际 staging/生产只读接入、完整事故名册与原始时间、独立恢复/安全复核、经过评审的对照方案、目标硬件上的代表性负载和持续运行记录。当前仓库没有这些证据。
