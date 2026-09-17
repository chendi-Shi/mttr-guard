# 本次验证记录

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
