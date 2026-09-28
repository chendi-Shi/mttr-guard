# v0.5.2：外部检查点与审计历史防回退

## 保护什么

HMAC 完整性校验能发现记录或状态被改动，却无法仅凭当前数据库判断一份签名完好的旧快照是不是最新历史。v0.5.2 增加两个参照：

- 每个 `PilotStore` 实例记住已验证的历史链头，以及本实例成功提交后的链头。后续事务必须包含这个历史前缀；历史长度缩短、相同序号换成另一条有效签名的分支，都会被拒绝。
- API 与 worker 可以通过 `--audit-checkpoint PATH` 固定一个独立保存的检查点文件。每个事务重新读取并验证它；进程重启后也不能接受早于该检查点的数据库。

这些校验保留原有全链和业务状态校验，不以链头替代完整检查。数据库加锁后才核对检查点和使用业务数据。进程内的提交链头只在 COMMIT 成功后更新；业务异常和 COMMIT 失败不会把未提交历史错误地变成最低要求。

检测失败后沿用审计隔离：运行中的 API 返回 `503 / audit_integrity_failed`，worker 不再领取或保存任务，进程保持隔离直至人工核对后重启。启动时不匹配的实例直接拒绝运行。

## 初次建立检查点

先按现有手册启动新数据库，完成一次受控操作并核对审计、事件来源和环境。零记录的审计库不能导出检查点。使用稳定的 `MTTR_AUDIT_KEY_FILE` 或 `MTTR_AUDIT_KEY`：

```powershell
python -m mttr_guard.enterprise audit-checkpoint --db artifacts/enterprise.db --output artifacts/checkpoints/initial.json
```

命令在只读数据库快照中完整验签后，导出 `{ "seq": ..., "signature": "..." }`。文件不包含事件正文或审计密钥。输出采用临时文件、fsync 和硬链接发布，拒绝覆盖现有文件。

导出动作本身不等于独立可信保存。请通过团队已有的受控配置/审计流程，把核对后的文件发布到独立目录或异地不可改写存储，再将可信副本提供给运行进程。限制 API/worker 身份为只读，检查点发布者与数据库恢复操作者应分权。不要只把检查点和数据库一起放入同一个可回退备份。

## API 与 worker 启用

在三个启动命令中都加入同一个可信检查点路径，例如：

```powershell
python -m mttr_guard.enterprise api --config config/enterprise.local.json --db artifacts/enterprise.db --audit-checkpoint C:\secure\mttr-checkpoints\current.json
python -m mttr_guard.enterprise worker --queue diagnose --config config/enterprise.local.json --db artifacts/enterprise.db --audit-checkpoint C:\secure\mttr-checkpoints\current.json
python -m mttr_guard.enterprise worker --queue observe --config config/enterprise.local.json --db artifacts/enterprise.db --audit-checkpoint C:\secure\mttr-checkpoints\current.json
```

该选项兼容已有审计库，无需新增表或迁移。未指定时保留进程内历史记忆，但**重启后的旧库回退检测没有外部保证**；默认命令不会自动伪造一个“可信”检查点。

容器部署时，为 API、diagnose、observe 三个服务都挂载同一个检查点**目录**，设置 `read_only: true`，并向各自 command 追加 `--audit-checkpoint /checkpoints/current.json`。挂载目录可让同目录原子替换的新文件在容器内可见；不要依赖单文件 bind mount 的 inode 替换行为。当前基础 Compose 保持兼容，不会替用户假定检查点的可信来源。

文件最大 4096 字节，仅允许 `seq` 和 `signature`；序号必须为正整数，签名为 64 位小写十六进制。重复字段、布尔/浮点序号、空历史、未知字段、过大文件、损坏或不可读取文件均拒绝。运行中已接受过的新检查点不能倒退；同序号换签名同样拒绝。没有缺失时自动跳过、使用陈旧文件或自动重新生成的路径。

## 推进与备份恢复

定期推进的频率由团队的审计/RPO 要求确定。先携带上一份可信检查点生成新文件：

```powershell
python -m mttr_guard.enterprise audit-checkpoint --db artifacts/enterprise.db --checkpoint C:\secure\mttr-checkpoints\current.json --output artifacts/checkpoints/next.json
```

独立发布方核对后，把新文件发布为运行目录的 `current.json`。应使用同目录临时文件加原子替换，避免应用读到半份文件；保留旧检查点的版本历史。应用在下一个事务采用新检查点，无需重启。新文件的历史必须已存在于正在服务的数据库，不要把其他环境或超前副本的检查点发布到此处。

备份和恢复现在也可以强制验证独立检查点：

```powershell
python -m mttr_guard.enterprise backup --db artifacts/enterprise.db --output artifacts/backups/verified.db --checkpoint C:\secure\mttr-checkpoints\current.json
python -m mttr_guard.enterprise restore --db artifacts/backups/verified.db --output artifacts/restored.db --checkpoint C:\secure\mttr-checkpoints\current.json
python -m mttr_guard.enterprise audit-verify --db artifacts/restored.db --checkpoint C:\secure\mttr-checkpoints\current.json
```

若备份早于最新可信检查点，恢复会失败且不发布目标数据库。这是丢失历史的信号，不能通过删除检查点来冒充完整恢复。需要按事故流程确定数据缺口、选择足够新的备份或重建新环境，并独立记录经过批准的恢复决策。应用没有自动降低检查点或自动重新建立信任的功能。

## 实际保证与限制

检查点是可信历史的最低要求，不保证这之后的每一条记录都已异地保存。活着的进程能识别已见历史回退；全部进程重启后，只能保证外部检查点所覆盖的前缀。独立检查点越旧，重启后仍可能丢失而无法识别的尾部越长。

进程记忆和 JSON 文件都不是不可改写存储。若数据库和检查点一起被回退并重启，或运行身份能改代码/进程内存，仍不能依靠本功能抵御。外部文件的来源、访问控制、异地保存、发布审批和定期推进由部署方负责，目前没有实现远端 WORM 服务或自动外发。内存中的链头只用于比较，不导出密钥；运行时检查点也不会传入取证/模型子进程。

每个事务仍扫描全审计链并持有 SQLite 写锁，增加的外部文件读取也有成本。这不是高可用、跨机一致性或生产容量验证。升级应覆盖所有 API/worker；新保护只对执行新版本代码的实例生效。

## 可复现验证

`python -m unittest discover -s tests -p test_checkpoints.py -v` 覆盖旧快照、同序号合法分支、热推进/降级、缺失文件、缓存开关、并发提交、事务与 COMMIT 失败、worker 停止、CLI 导出和备份恢复拒绝。API 测试确认旧库仍可独立验签，但在线探针、查询和人工操作全部返回 503 且不追加审计。

`python -m scripts.run_enterprise_drill` 为三个真实子进程启用检查点，热更新检查点并继续正常恢复、重启、备份还原和篡改隔离流程。演练全部使用临时目录中的 DEMO 数据；临时目录不构成独立信任根，也不代表生产准入。
