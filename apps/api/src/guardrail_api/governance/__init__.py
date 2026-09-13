"""治理内核(W2)。

按职责拆开,每一块都能单独测:

- `trace`        trace_id 的上下文贯穿
- `idempotency`  幂等账本:抢占 / 复用 / 接管
- `audit`        追加写审计
- `lease`        租约领取 / 心跳 / 退避
- `executor`     把计划变成可恢复、不重复、可审计的写操作
- `worker`       拉取式的执行进程
"""
