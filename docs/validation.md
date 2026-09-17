# 本次验证记录

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
