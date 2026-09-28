# v0.4：单机加固的只读试点

本版本补的是让值班团队持续使用、可恢复和可追查的运行基础。API、诊断 worker、恢复观察 worker 分开运行；慢诊断不占用观察队列。接入真实只读证据后，值班人员可以集中核对、记录处置、交接事件，恢复由连续健康窗口确认。

**当前仍是 SQLite 单机试点，不是多机高可用平台。没有真实发布写入、真实企业 IAM 联调、压测或线上 MTTR 对照结果。** `enterprise` 是命令模块名，不代表企业级认证。旧的 `pilot demo` 继续用于体验；持续试点使用本手册入口。

## 已实现的运行行为

- API 使用 FastAPI/Uvicorn，默认绑定回环地址，API 与 worker 生命周期独立。请求体限制 1 MiB、接收限时 10 秒，单进程并发限制 64；按连接源地址做基础限流。反向代理后的请求共享代理地址，生产入口应另外设置合适的限流规则。
- 诊断、观察分队列；数据库租约支持重启接续，旧 worker 的结果不能覆盖新租约。单次取证/分析放在独立子进程中，70 秒总期限后终止并回收。子进程只继承必要系统环境和配置指定的只读采集凭据；这不是文件系统安全沙箱。
- v0.5.1 将崩溃遗留的租约也计入每轮诊断三次尝试预算；观察到期后停止新采集，迟到结果不能记为恢复。终止任务转为人工处理，支持现有 operator 重新收集证据入口，并以 `mttr_guard_needs_attention` 暴露按服务隔离的待处理数量。升级和接续流程见 [任务退出与人工接续](job-recovery.md)。
- v0.6 增加服务诊断/观察队列各自的容量与租约并发上限、跨服务持久化轮流调度、503 重试提示及容量指标。旧数据库自动增加调度表，必须统一更新所有 API/worker 和队列配置；规则、具体解决的问题及剩余限制见 [队列隔离手册](queue-isolation.md)。
- 操作者可暂停观察、交接给同服务的已配置 operator，再记录处置并重启观察。并发交接检查原接手人，防止静默覆盖。交接不发送通知。
- HMAC-SHA256 审计链绑定工作流事件和最新事件状态，访问与拒绝也记录审计。业务变更与关联审计在同一事务提交；日志不输出 Bearer、完整请求体或模型异常内容。
- v0.4.1 在取得 SQLite 事务锁后、使用存量数据前校验完整历史，覆盖人工操作、查询、告警去重、后台取证结果和访问审计。v0.4.2 保留全量扫描，对原文完全相同的已验证记录复用密码学校验结果；每进程默认 16 MiB 估算缓存预算，可用 `--audit-cache-mib 0` 关闭。校验失败时当前进程隔离存储；不会通过再次保存数据掩盖篡改。见 [审计完整性手册](audit-integrity.md) 和 [缓存容量边界](audit-cache.md)。
- v0.5.2 在运行进程中记住已验证/提交的历史；可选 `--audit-checkpoint PATH` 每个事务核对独立保存的历史下限，重启时也拒绝早于检查点的数据库。检查点缺失、损坏或后退时拒绝读写。可信发布、更新、容器挂载和恢复步骤见 [外部审计检查点](audit-checkpoints.md)。
- `/healthz` 检查存储，`/readyz` 还检查有效配置及两类 worker 心跳覆盖。`/api/operations` 和 `/metrics` 需要 viewer/operator 身份，按服务返回队列、过期任务等运行指标。150 秒未更新的 worker 不计为存活。
- 在线备份生成数据库与校验清单；恢复前检查文件哈希、SQLite 完整性和审计链，只能写入新路径。恢复会重置只读采集任务的 RUNNING 租约，不重试模拟执行副作用。

## 配置、身份与密钥

从 `config/enterprise.example.json` 复制为 `config/enterprise.local.json`。服务的五维作用域、PromQL、监控/证据网关地址和恢复阈值，按 [只读试点契约](read-only-pilot.md) 配置。示例中 `/auth/receiver.token`、`/auth/operator.token` 是容器内路径；直接在 Windows 运行时改为本地绝对路径。

配置使用三类身份：receiver 只投递告警；viewer 只查看；operator 可核对、接手、交接和启动/暂停观察。每个身份只绑定指定服务。机器令牌须随机、至少 32 字符；`token_file` 和 `token_env` 二选一。API 每次授权重新读配置和令牌文件，撤销或替换立即对后续请求生效；配置损坏时拒绝请求。修改配置或令牌文件应采用同目录临时文件加原子替换，避免读到半写入内容。不要把凭据提交到 Git。

示例还包含可选 `oidc`。未接 IdP 时删除该字段，保留本地令牌；接入时替换 HTTPS issuer、固定 JWKS 地址、API audience 和具体用户 subject 绑定。JWT 仅接受 RS256、至少 2048 位 RSA、`typ: at+jwt`，并要求 `iss/aud/sub/iat/nbf/exp`。默认最长有效期 900 秒，可配置 60–3600 秒。用户角色来自本地 bindings，不采信 JWT 自带的 roles/groups。持有多个服务绑定时使用 `X-MTTR-Service`，工作台登录页有对应输入框。

这是 **API access token 校验**，尚未实现浏览器跳转登录、授权码/PKCE 或 IdP 自动发现。不要填写 ID token。JWKS 缓存 60 秒，未知 kid 的强制刷新最短间隔 10 秒；缓存过期后无法取钥会拒绝认证。撤销本地绑定立即生效；仅在 IdP 移除签名密钥的生效时间受缓存影响。OIDC 测试使用真实 RSA 签名和模拟 JWKS，未与企业 IdP 联调。

审计密钥通过 `MTTR_AUDIT_KEY_FILE` 指向一个至少 32 字符的高熵固定密钥文件，也可以使用 `MTTR_AUDIT_KEY`，两者不能同时设置。三个进程和备份恢复命令必须使用相同密钥。首次运行请使用**新数据库**；不要把 v0.2/v0.3 演练数据库直接作为已完成历史审计迁移的数据。已初始化的审计库拒绝换钥启动，当前不提供密钥轮换迁移程序。

## 本地三个进程

Python 3.11+，建议独立虚拟环境。以下为 PowerShell，在仓库根目录运行；各终端均须加载自己的配置所需环境变量。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-enterprise.txt

# 在三个终端均设置，指向你已准备的稳定密钥文件
$env:MTTR_AUDIT_KEY_FILE = 'C:\secure\mttr\audit.key'
# 同样提供配置指定的 MTTR_PROMETHEUS_TOKEN / MTTR_EVIDENCE_TOKEN

# 终端 1
.\.venv\Scripts\python.exe -m mttr_guard.enterprise api --config config/enterprise.local.json --db artifacts/enterprise.db
# 终端 2
.\.venv\Scripts\python.exe -m mttr_guard.enterprise worker --queue diagnose --config config/enterprise.local.json --db artifacts/enterprise.db
# 终端 3
.\.venv\Scripts\python.exe -m mttr_guard.enterprise worker --queue observe --config config/enterprise.local.json --db artifacts/enterprise.db
```

打开 `http://127.0.0.1:8787`。端口若被 demo 占用，为 API 指定 `--port 8788` 并访问相应端口。不要把演练证据当成真实服务证据。worker 默认规则推理；仅诊断 worker 可另外传 `--model YOUR_MODEL --ollama-url YOUR_ENDPOINT`。接真实模型前需落实证据脱敏及质量评估。

## 容器监督与监控

`deploy/compose.yml` 定义同主机三进程、共享本地数据卷、非 root 用户、只读容器根目录、资源上限和退出自动重启。镜像基础与 Python 依赖均固定版本。这里使用本地 SQLite 卷，**不能复制到多个主机或改成共享网络文件系统来宣称高可用**。

将服务 JSON 放在专用配置目录；身份令牌放在独立目录，收紧文件权限，并确保容器 UID 10001 可读取必要文件。只读采集凭据放入专用 env 文件。然后设置 Compose 使用的四个路径：

```powershell
$env:MTTR_CONFIG_DIRECTORY = 'C:\secure\mttr\config'
$env:MTTR_AUTH_DIRECTORY = 'C:\secure\mttr\auth'
$env:MTTR_CONNECTOR_ENV_FILE = 'C:\secure\mttr\connector.env'
$env:MTTR_AUDIT_KEY_PATH = 'C:\secure\mttr\audit.key'
docker compose -f deploy/compose.yml config --quiet
docker compose -f deploy/compose.yml up --build -d
```

配置目录内文件名必须为 `enterprise.local.json`。env 文件仅放只读采集变量，不放另一份审计密钥变量。API 仅映射到宿主回环地址；远程访问需接现有 HTTPS 网关，正确转发 Authorization 并限制入口。Uvicorn 不信任转发的客户端地址，不能依赖 `X-Forwarded-For` 绕过授权/限流。Compose 健康检查失败只标记 unhealthy，不会单独触发重启；退出重启也不等于宿主机故障切换。

Prometheus 抓取 `/metrics` 须配置服务对应的 viewer 令牌；多服务须各自抓取。`deploy/alerts.yml` 提供 API 不可达、worker 不就绪、恢复观察积压的规则草案，需要按实际 scrape job、告警接收渠道及负载调整。文件未部署，也不会自行向值班人员发消息。

本机验证了 Compose 配置结构与基础镜像清单，**Docker daemon 未运行，未构建/启动容器**。首次部署还需验证镜像依赖安装、挂载权限、健康检查、TLS 和资源限制。

## 备份、审计验证与恢复

同一环境配置好审计密钥后运行：

```powershell
python -m mttr_guard.enterprise backup --db artifacts/enterprise.db --output artifacts/backups/pilot-20260917.db
python -m mttr_guard.enterprise audit-verify --db artifacts/enterprise.db
python -m mttr_guard.enterprise restore --db artifacts/backups/pilot-20260917.db --output artifacts/restored.db
```

备份命令可以在服务运行时执行，30 秒总期限。产出 `pilot-20260917.db` 和同名 `.db.json` 清单；两者一起保存。输出路径必须全新，文件系统需支持硬链接；Windows NTFS 可用。失败时不要把不完整备份当作成功，也不要覆盖原数据库。当前没有内置定时备份、加密或异地复制。

完整恢复流程：先把旧 API/worker 停止，保留故障现场；还原到新路径，独立核对审计和数据，再把全部三个进程切换到新路径。等待 `/readyz` 返回就绪，投递一个受控测试告警，检查接收、取证及恢复观察。不要让新旧数据库同时接收真实告警。RPO 取决于最近成功的异地备份；RTO 需要团队在自己的部署环境演练测量。

审计是**可检测篡改的本地链，不是不可改写存储**。校验覆盖事件、状态和作用域/状态索引，并检查未审计的额外事件；worker 心跳、内部租约等调度元数据不在签名范围内。v0.5.2 增加 `audit-checkpoint` 导出、API/worker 的 `--audit-checkpoint` 参数，以及备份/恢复/离线验证的 `--checkpoint` 参数。请将检查点经独立流程保存和发布到受保护目录；项目不会自动发送到远端存储。备份清单与备份一起被回退时不能替代独立锚点。掌握运行环境、数据库、密钥且能重置独立检查点的人仍超出此保护范围。详细保证和失效处置见 [外部审计检查点](audit-checkpoints.md)。

## 验收与下一阶段

v0.5 增加实际证据源 preflight、运行库审计导出、固定方案的完整事件评估和限速 HTTP 探测。先阅读 [真实接入与效果验收](real-world-validation.md)，在自己的部署和数据上运行；合成演练通过不等于这些真实验证已完成。

```powershell
python -m unittest discover -s tests -v
python -m scripts.run_enterprise_drill
# 可选：本机安装 Node、Playwright 和 Edge 后
python -m scripts.run_enterprise_drill --browser-check
```

演练启动临时 API 和两个独立 worker，接入本地 DEMO 证据，强制终止并重启观察 worker，随后验证三个健康窗口、在线备份、还原和审计。最后通过独立连接篡改临时数据库，检查仍在运行的 API 返回 503、正常接手不能掩盖篡改、后台轮询不再修改数据。可选页面检查验证暂停、交接、恢复观察和移动布局。报告保存在 `artifacts/enterprise-drill.json`；这些是合成数据，不能作为真实 MTTR 降幅。

下一阶段先选一个 staging 服务，完成 Prometheus/证据网关/IdP/TLS 实际联调及备份恢复演练。之后收集真实告警到接手、建议、人工动作、验证恢复的时间，保留未恢复事件，与同服务历史或匹配事件对照；报告样本量和分布，避免只看已恢复平均值。再依据压测和运行数据决定 PostgreSQL、多副本协调、独立审计存储及企业通知/审批平台接入。真实部署写入连接器需单独验证远端幂等、版本 CAS 和结果未知对账后再开放。
