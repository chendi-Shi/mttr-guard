# 本次验证记录

验证日期：2026-09-17。环境：Windows、Python 3.14.7、SQLite 3.53.2。

## 已运行

- `python -m unittest discover -s tests -v`：29 项测试通过。
- `python -m mttr_guard --simulate-approval --output artifacts/release-report.json`：灰度回归场景生成待审批方案，显式模拟审批后回退到 r41，三次后续合成观测确认恢复。
- `python -m mttr_guard --scenario downstream`：下游故障升级人工，不生成可执行回退方案。
- `python -m mttr_guard --scenario migration`：迁移兼容性不满足，升级人工。
- `python -m mttr_guard --scenario missing`：缺少调用链证据，升级人工。

测试包括作用域/角色限制、审批哈希不匹配、未经审批执行、审批与执行身份分离、证据篡改、审批过期、版本漂移、预检数据过期、kill switch、重启后幂等回执、12 次并发执行仅一次模拟副作用、模型失败/超时/伪造引用、过大响应、恢复观察缺窗、流量归零及验证超时。

## 结果边界

执行副作用仅限模拟数据库；这些测试不能证明远端发布副作用的幂等性。模型适配器使用 mock HTTP 验证接口契约，没有连接真实 Ollama 模型。没有进行线上监控集成、IAM/审批回调集成、实际发布、负载测试、跨进程故障演练或 MTTR 对照实验。

演示的恢复耗时为虚拟时钟计算值，不能用作真实 MTTR。完整事件报告位于本地 `artifacts/`，属于合成数据且被 Git 忽略。
