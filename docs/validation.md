# 本次验证记录

## v0.6 多服务队列隔离与公平调度

2026-09-28，Windows / Python 3.14.7，固定 enterprise 依赖：

- **做了什么**：服务诊断/观察队列独立积压额度、有效执行租约上限、跨服务持久化轮流调度，以及 HTTP 队列满响应和容量指标。
- **怎么做**：以完整五维 scope 和任务 kind 计数；在同一个 SQLite 写事务内检查额度、更新调度轮次、领取租约和写入审计。人工重新诊断/观察也检查额度，重复投递不重复占额，队列额度调整不改变旧事件的 collection hash。
- **解决什么问题**：单服务告警洪峰占满全局队列、老积压挡住其他服务新事故、多个 worker 同时领取导致同服务采集并发过高，以及人工重试绕过限额。
- 完整 `python -m unittest discover -s tests -v`：206 项通过，53.655 秒。新增 20 项，覆盖服务洪峰、批次原子性、重复接收、全局容量、轮次与排序、租约过期、12 个实例并发竞争、4 个真实子进程共享并发上限、配置热调整、人工操作幂等、事务回滚、旧库建表、备份还原和 API/指标作用域。
- `python -m scripts.run_enterprise_drill`：49.32 秒通过。API、诊断和观察三个真实进程使用新调度器完成 DEMO 恢复、观察 worker 重启、三个健康窗口、检查点热推进、135 条审计记录的备份还原与运行中篡改隔离。

Git diff 空白检查通过。新增调度元数据表 `pilot_dispatch`，不修改依赖、界面或容器配置。现有 Linux/Windows × Python 3.11/3.14 CI 会执行全部新增测试；远程结果以本次提交为准。详细配置、保证和剩余限制见 [队列隔离手册](queue-isolation.md)。这些结果不表示生产吞吐、物理请求严格并发界限、多机高可用或真实 MTTR 改善。

## v0.5.2 外部审计检查点与历史防回退

2026-09-28，Windows / Python 3.14.7，固定 enterprise 依赖：

- 完整 `python -m unittest discover -s tests -v`：186 项通过，47.628 秒。新增 18 项，覆盖签名有效的旧快照、相同序号的合法分支、成功提交立即记忆历史、并发提交、业务回滚和真实 SQLite COMMIT 失败、外部文件热推进/降级/缺失/损坏、缓存开关、worker 隔离、CLI 导出、拒绝旧备份和 API 503。
- `python -m scripts.run_enterprise_drill`：49.10 秒通过。API 与两个真实 worker 子进程都启用外部检查点，推进检查点后无需重启即可继续运行；DEMO 最终 RECOVERED，观察 worker 重启、三个健康窗口、130 条审计记录的备份还原及运行中篡改隔离均通过。报告新增 `external_checkpoint_enabled` 和 `checkpoint_advanced_without_restart`，均为 true。
- 旧库拒绝测试通过独立 SQLite 连接构造旧版本夹具；生产代码仍拒绝直接接纳无审计的旧历史。没有修改依赖、界面或容器配置；新增部署文档描述独立检查点目录的只读挂载。未在真实企业存储中发布检查点。

Git diff 空白检查通过。所有新增测试由现有 Linux/Windows × Python 3.11/3.14 CI 自动执行，远端结果以对应提交为准。临时目录中的演练检查点不是独立信任根；本轮不构成生产容量、多机高可用或真实 MTTR 验收。使用方式及信任边界见 [外部审计检查点](audit-checkpoints.md)。

## v0.5.1 有界任务重试与人工接续

2026-09-21，Windows / Python 3.14.7，固定 enterprise 依赖：

- 完整 `python -m unittest discover -s tests -v`：168 项通过，28.301 秒。新增 14 项，覆盖三次诊断预算在采集失败和崩溃之间共享、三个真实子进程领取后突然退出、有效第三次租约保护、并发重启、旧结果隔离、观察期限、审计写入失败整体回滚、备份还原保留次数、operator 重试幂等及指标作用域。
- `python -m scripts.run_enterprise_drill`：40.13 秒通过。API、诊断和观察三个进程完成 DEMO 恢复、观察 worker 重启、三个健康窗口、137 条审计记录的备份还原，以及运行中篡改后 API 拒绝访问、数据库不再变化、离线审计仍报损坏。本机四请求 HTTP 探测全部成功，仅验证接口功能。
- 未修改依赖、界面或容器配置；没有重复浏览器或容器验证。新告警规则为待部署配置，尚未向实际监控系统加载，也未发送通知。

时间推进与故障输入由测试控制；结果验证任务退出规则和回归兼容性，不表示实际 MTTR、生产容量或多机高可用。GitHub 的 Linux/Windows × Python 3.11/3.14 自动运行这些新增用例，远端结果以相应提交的 Actions 为准。操作边界见 [任务退出与人工接续](job-recovery.md)。

## v0.5 真实接入与效果验收工具

2026-09-18，Windows 11 / Python 3.14.7，固定 enterprise 依赖：

- 完整 `python -m unittest discover -s tests -v`：154 项通过，33.770 秒。新增 27 项，覆盖完整/缺失/未恢复事件、早期失访界限、固定分层权重、输入哈希与检测起点、模拟域混入、独立安全复核、审计导出、预检拒绝 DEMO、探测超时/重定向/过载/非法响应及凭据输出边界。
- `python -m scripts.run_validation_drill`：真实 CLI 处理完整 SYNTHETIC 数据返回 0；缺失结果仍保留 16 个登记事件、生成证据不足报告并返回 2；输入哈希被改动时返回 1 且不生成通过报告。所有示例均明确为合成输入，报告不声明因果 MTTR 改善。
- `python -m scripts.run_enterprise_drill`：41.16 秒完成。三个真实子进程完成 DEMO 恢复、观察 worker 重启、三个健康窗口、124 条审计记录的备份还原与运行中篡改隔离。新增本机 HTTP 功能探测按计划发出 4 次请求，全部 200，未跳过或失败；这四次请求仅验证工具可工作，不能作为容量压测结论。
- Git diff 空白检查通过。CI 已加入评估 CLI 演练，现有三进程演练同时验证 HTTP 探测；远端矩阵结果以对应提交的 Actions 为准。没有 UI、容器或依赖改动。

尚未收到真实环境接入或真实事故数据，没有运行真实 Prometheus/证据网关 preflight、企业 IdP 联调、目标硬件容量验证、真实事件对照或线上因果实验。当前交付的是可用于这些工作的工具，不是生产验收结论。具体输入和操作见 [真实接入与效果验收](real-world-validation.md)。

## v0.4.2 校验缓存与性能对照

2026-09-18，Windows 11 / Python 3.14.7，enterprise 固定依赖：

- `python -m unittest discover -s tests -v`：127 项全部通过，15.118 秒；新增 11 项缓存回归测试（含多个篡改子场景），覆盖原文/数值类型变化、预算不足、未缓存记录、其他实例追加、事务回滚、失败不更新缓存、密钥绑定及外部检查点。
- `python -m scripts.run_enterprise_drill`：37.81 秒通过。API 与两个 worker 默认启用缓存，DEMO 恢复、三个健康窗口、观察进程重启、142 条审计记录的备份还原均成功；运行中篡改后 API 拒绝读写、数据库保持不变、离线仍能识别篡改。
- 同一脚本分别用 `--cache-mib 0` 和 `--cache-mib 16`，测量 100 个合成事件、1,000/10,000/25,000 条历史、每档 10 次查询加访问审计。P50 分别从 78.986/438.220/1,023.232 ms 降至 19.930/97.361/637.093 ms。冷启动与预算耗尽的代价已记录，完整命令和 P95 见 [校验缓存说明](audit-cache.md)。
- Git diff 空白检查通过。未修改 UI 或依赖，没有重复浏览器/容器验证。远程 Linux/Windows × Python 3.11/3.14 的状态以对应提交的 Actions 结果为准。

这些是热态、单进程审计开销与合成故障演练，没有真实流量、并发容量或线上 MTTR 结论。事务仍扫描全部历史并持有 SQLite 写锁；缓存并未提供高可用或独立的审计信任根。

## v0.4.1 运行时审计完整性修复

2026-09-18，Windows 11 / Python 3.14.7，独立虚拟环境安装 enterprise 固定依赖：

- `python -m unittest discover -s tests -v`：116 项通过，14.507 秒。新增 10 项测试（含多个子场景），验证正常操作不能重新签署篡改状态、多实例读写拒绝、审计结构损坏、旧状态重放、采集中篡改、进程隔离、并发写入锁，以及 HTTP 503 和响应内容边界。
- `python -m scripts.run_enterprise_drill`：37.48 秒完成，DEMO 最终 RECOVERED、三个健康窗口、观察 worker 强制重启、备份/还原审计通过。之后在三个进程仍运行时篡改临时数据库：API 拒绝读取/接手，等待后台轮询后数据库保持不变，离线审计仍判定失败。报告字段 `live_integrity_quarantine` 的三项检查均为 true。
- `python -m scripts.benchmark_audit_guard`：100 个合成事件、单进程、每档 10 次查询加访问审计；1,000/10,000 条历史记录时 P50 为 83.396/494.975 ms，P95 为 95.799/840.184 ms。完整口径及限制见 [审计完整性手册](audit-integrity.md)。这是审计开销测量，不是系统容量或真实 MTTR 验证。
- Git diff 空白检查通过。本轮没有修改 UI、依赖或容器配置，没有重复浏览器和容器验证。远程矩阵以对应提交的 GitHub Actions 结果为准。

运行时校验采用全量重放并持有 SQLite 事务锁，有明确的历史规模成本。仍未完成真实 IdP/证据源联调、多机高可用、独立审计锚点及线上 MTTR 对照，不能将这一安全修复描述为全面生产准入。

## v0.4 单机加固增量验证

2026-09-17，Windows / Python 3.12.14，独立虚拟环境安装固定版本的 enterprise 依赖：

- 最终全套 `python -m unittest discover -s tests -q`：106 项通过，26.143 秒。包含原有 80 项，以及 OIDC RSA 签名/issuer/audience/算法混淆/时效/服务范围、密钥缓存失败、绑定撤销、令牌文件轮换、API 角色边界与限流、审计篡改检测（含未审计插入及作用域索引改写）、拒绝静默迁移旧历史、外部链头检测整库回退、备份恢复、队列隔离和真实子进程终止。
- `python -m scripts.run_enterprise_drill`：实际启动 Uvicorn、诊断 worker、观察 worker 三个进程；杀死并重启观察 worker 后完成三个健康窗口，在线备份并还原成功，HMAC 审计验证通过。一次运行 34.95 秒，备份包含 136 条审计记录和 1 起 DEMO 事件；记录数量会随轮询改变。
- `pip check` 通过。Compose 静态配置检查通过，基础镜像清单已核实；Docker daemon 不可用，未进行容器构建或运行验证。
- CI 配置增加安装 enterprise 依赖后的全套测试及三进程演练；未推送，远程矩阵尚未验证。
- `python -m scripts.run_enterprise_drill --browser-check`：Playwright / Edge headless 验证登录、暂停、交接、重启观察和退出，1440 像素桌面及 390 像素移动宽度无水平溢出或页面脚本错误；页面不把令牌写入浏览器存储。完整演练 36.24 秒，最终 RECOVERED、3 个健康窗口、备份还原审计通过。截图在 `artifacts/enterprise-ui/`。

OIDC 的 JWKS 为测试替身；没有真实 IdP 联调、真实证据源对接、多机切换、压力测试或线上效果实验。HMAC 本地审计不是 WORM 存储，备份未自动异地复制。演练时间不是生产 MTTR。部署和限制见 [v0.4 手册](production-pilot.md)。

## v0.3 只读试点增量验证

2026-09-17，Windows / Python 3.12.14：

- `python -m unittest discover -s tests -v`：80 项测试全部通过，包括保留的 50 项以及新增的 30 项试点测试。
- 新测试覆盖整批入库与队列原子性、重复/并发投递、队列上限、租约过期和旧 worker 写入隔离、采集失败重试、作用域与角色授权、指标异常/新鲜度、建议过期、配置漂移、恢复缺窗、流量归零、版本不符、未恢复与 DEMO 统计口径。
- `python -m scripts.run_pilot_drill`：完整 HTTP 演练进入 RECOVERED，三个健康窗口，重建 worker 后持续观察；一次运行墙钟约 32.76 秒。**全部是 DEMO，不是生产 MTTR 或改善比例。**
- `node scripts/check_pilot_ui.cjs`：通过 Playwright / Edge headless 验证登录、接手、审核、记录外部演练动作、恢复验证、退出；1440 像素桌面及 390 像素移动宽度通过，无浏览器脚本错误或水平溢出。需本机可用 Playwright 与 Edge；该检查不是 Python 核心依赖。
- JavaScript 语法检查和 Git diff 空白检查通过。CI 已添加 HTTP 演练步骤；本地修改不等于远程矩阵已运行。

未进行真实 Prometheus 服务对接、企业证据网关对接、IAM/审批平台集成、负载压测、真实模型评估或线上效果对照。真实使用入口和范围见 [只读试点](read-only-pilot.md)。

## v0.2 原始验证记录

验证日期：2026-09-17。环境：Windows、Python 3.14.7、SQLite 3.53.2。

## 已运行

- `python -m unittest discover -s tests -v`：v0.2 共 50 项测试通过；初版为 29 项。
- `python -m mttr_guard --simulate-approval --output artifacts/release-report.json`：灰度回归场景生成待审批方案，显式模拟审批后回退到 r41，三次后续合成观测确认恢复。
- `python -m mttr_guard --scenario downstream`：下游故障升级人工，不生成可执行回退方案。
- `python -m mttr_guard --scenario migration`：迁移兼容性不满足，升级人工。
- `python -m mttr_guard --scenario missing`：缺少调用链证据，升级人工。
- `python -m mttr_guard --simulate-approval --fault after_submit`：独立发布端完成后丢响应，对账后恢复，远端模拟副作用一次。
- `python -m mttr_guard --simulate-approval --fault before_submit`：请求未送达，查询返回 NOT_FOUND，保留 EXECUTION_UNKNOWN，远端副作用零次。

新增测试真实启动子进程并用 `os._exit` 终止，分别验证持久化意图之后/发送之前，以及独立发布库提交之后的崩溃窗口。还覆盖远端版本 CAS、请求幂等冲突、并发事件互斥、重复派发、撤销审批、回执校验和固定恢复策略。

测试包括作用域/角色限制、审批哈希不匹配、未经审批执行、审批与执行身份分离、证据篡改、审批过期、版本漂移、预检数据过期、kill switch、重启后幂等回执、12 次并发执行仅一次模拟副作用、模型失败/超时/伪造引用、过大响应、恢复观察缺窗、流量归零及验证超时。

## 结果边界

执行副作用仅限独立模拟数据库；这些测试不能证明真实发布 API 的幂等性。模型适配器使用 mock HTTP 验证接口契约，没有连接真实 Ollama 模型。没有进行线上监控集成、IAM/审批回调集成、实际发布、宿主机/磁盘故障演练、负载测试或 MTTR 对照实验。

GitHub 自动测试矩阵已定义 Linux/Windows × Python 3.11/3.14，远程运行状态以 Actions 页面为准；本段本地验证记录不等于所有远程环境均已验证。

演示的恢复耗时为虚拟时钟计算值，不能用作真实 MTTR。完整事件报告位于本地 `artifacts/`，属于合成数据且被 Git 忽略。
