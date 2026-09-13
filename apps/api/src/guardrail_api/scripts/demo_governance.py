"""治理内核演示:把 W2 的三条验收演一遍。

     uv run python -m guardrail_api.scripts.demo_governance

会依次发生(每一步都会把真实数据打出来,不是旁白):

1. 登记一次 3 步的执行:查订单 → 退款订单 A → 退款订单 B
2. worker 跑完前两步后被 **真的 SIGKILL**(`os.kill(pid, SIGKILL)`)
3. 重启 worker:从 checkpoint 接着跑第 3 步,前两步不重放
4. 对第 2 步重放 5 次:业务表只多一条,审计只多一行
5. 打印第 2 步的审计前后值

它跑的是和生产同一套代码,区别只是多了一个混沌钩子。
"""

import asyncio
import os
import subprocess
import sys

from sqlalchemy import func, select

from guardrail_api.db import dispose_engine, get_session_factory
from guardrail_api.governance.executor import PlannedStep, RunExecutor, create_run
from guardrail_api.models import (
    AftersalesTicket,
    AgentRun,
    AgentStep,
    AuditLog,
    IdempotencyRecord,
    Order,
    OrderStatus,
    StepStatus,
)

WORKER = "guardrail_api.governance.worker"

#: 演示把租约压到 2 秒(生产默认 30 秒),否则「等租约过期」要等半分钟。
#: 变的只是时长,语义完全一致:持租约才能推进,续不上就得让别人接管。
DEMO_LEASE_SECONDS = 2


def title(text: str) -> None:
    print(f"\n\033[36m{'=' * 4} {text}\033[0m")


async def pick_refundable(factory, limit: int = 2) -> list[Order]:
    """挑一张工单都没有的已支付订单,避免演示被业务规则(可退额度)拦下。"""
    async with factory() as session:
        busy = set(
            (await session.execute(select(AftersalesTicket.order_id).distinct())).scalars().all()
        )
        rows = (
            (
                await session.execute(
                    select(Order).where(Order.status == OrderStatus.PAID).limit(400)
                )
            )
            .scalars()
            .all()
        )
        picked = [
            order for order in rows if order.id not in busy and order.total_amount_cents >= 1000
        ][:limit]
        if len(picked) < limit:
            raise SystemExit("演示数据不足,请先执行 make seed-reset")
        return [order.id for order in picked]


async def snapshot(factory, order_ids: list[int], run_uid: str) -> None:
    async with factory() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
        ).scalar_one()
        steps = (
            (
                await session.execute(
                    select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
                )
            )
            .scalars()
            .all()
        )
        ticket_total = await session.scalar(
            select(func.count())
            .select_from(AftersalesTicket)
            .where(AftersalesTicket.order_id.in_(order_ids))
        )
        print(
            f"  run={run.status.value:<9} checkpoint={run.checkpoint_seq}/3 "
            f"attempt={run.attempt} 工单数={ticket_total}"
        )
        for step in steps:
            mark = "✓" if step.status is StepStatus.SUCCEEDED else "✗"
            replay = " · 幂等重放" if step.replayed else ""
            print(
                f"    {mark} #{step.seq} {step.tool_name:<14} "
                f"status={step.status.value:<9} attempt={step.attempt}{replay}"
            )
        if run.last_error:
            print(f"    last_error={run.last_error}")


def run_worker(run_uid: str, worker_id: str, *extra: str) -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            WORKER,
            "--run",
            run_uid,
            "--once",
            "--worker-id",
            worker_id,
            *extra,
        ],
        check=False,
        env={
            **os.environ,
            "LEASE_SECONDS": str(DEMO_LEASE_SECONDS),
            "HEARTBEAT_INTERVAL_SECONDS": "1",
        },
    )
    return result.returncode


async def main() -> None:
    factory = get_session_factory()
    first, second = await pick_refundable(factory)

    title("1. 登记一次执行(此时没有任何副作用)")
    async with factory() as session:
        run = await create_run(
            session,
            goal="演示:kill -9 之后从断点接着跑",
            actor="agent:demo",
            plan=[
                PlannedStep(seq=1, tool="query_order", args={"order_id": first}),
                PlannedStep(
                    seq=2,
                    tool="create_refund",
                    args={
                        "order_id": first,
                        "reason_code": "QUALITY_ISSUE",
                        "amount_cents": 100,
                        "description": "演示:第 2 步",
                    },
                ),
                PlannedStep(
                    seq=3,
                    tool="create_refund",
                    args={
                        "order_id": second,
                        "reason_code": "DAMAGED_IN_TRANSIT",
                        "amount_cents": 100,
                        "description": "演示:第 3 步",
                    },
                ),
            ],
            trace_id="demo-trace",
        )
        await session.commit()
        run_uid = run.run_uid
    print(f"  run_uid={run_uid} 订单={first},{second}")
    await snapshot(factory, [first, second], run_uid)

    title("2. worker 跑完第 2 步后,在第 3 步事务开始前被 SIGKILL")
    code = run_worker(run_uid, "demo-w1", "--crash-before-step", "3")
    print(f"  worker 退出码={code}(-9 = 被 SIGKILL)")
    await snapshot(factory, [first, second], run_uid)

    title(f"3. 重启 worker:租约({DEMO_LEASE_SECONDS}s)到期后接管,从 checkpoint 继续")
    for _ in range(8):
        await asyncio.sleep(DEMO_LEASE_SECONDS + 0.5)
        code = run_worker(run_uid, "demo-w2")
        async with factory() as session:
            status = (
                await session.execute(select(AgentRun.status).where(AgentRun.run_uid == run_uid))
            ).scalar_one()
        if status.is_terminal:
            break
    print(f"  worker 退出码={code}")
    await snapshot(factory, [first, second], run_uid)

    title("4. 对第 2 步重放 5 次:副作用不会发生第二次")
    executor = RunExecutor(factory, worker_id="demo-replayer")
    for index in range(5):
        outcome = await executor.retry_step(run_uid, 2)
        print(
            f"    第 {index + 1} 次重放 → replayed={outcome.replayed} "
            f"ticket_no={(outcome.result or {}).get('ticket_no')}"
        )
    async with factory() as session:
        tickets = await session.scalar(
            select(func.count())
            .select_from(AftersalesTicket)
            .where(AftersalesTicket.order_id.in_([first, second]))
        )
        records = await session.scalar(
            select(func.count())
            .select_from(IdempotencyRecord)
            .where(IdempotencyRecord.run_uid == run_uid)
        )
        audits = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.run_uid == run_uid)
        )
    print(f"  工单数={tickets} · 幂等账本={records} · 审计行={audits}(重放不写审计)")

    title("5. 审计:第 2 步到底把什么改成了什么")
    async with factory() as session:
        entry = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.run_uid == run_uid, AuditLog.step_seq == 2)
                    .order_by(AuditLog.id)
                )
            )
            .scalars()
            .first()
        )
    if entry is None:
        print("  没有第 2 步的审计记录 —— 说明它一次都没真的执行成功")
        await dispose_engine()
        return
    before, after = entry.before or {}, entry.after or {}
    print(f"  actor={entry.actor} reason={entry.reason} trace={entry.trace_id}")
    before_count = len(before.get("tickets", []))
    after_count = len(after.get("tickets", []))
    print(f"  操作前工单数={before_count} → 操作后工单数={after_count}")
    print(f"  订单状态={before['order']['status']} → {after['order']['status']}")
    for ticket in after.get("tickets", []):
        print(f"    新增工单 {ticket['ticket_no']} status={ticket['status']}")

    title("6. 审计改不掉(数据库触发器兜底)")
    async with factory() as session:
        try:
            from sqlalchemy import text

            await session.execute(text("UPDATE audit_log SET reason = '篡改一下'"))
            print("  ✗ 居然改成功了,说明触发器没生效")
        except Exception as exc:
            print(f"  ✓ 被数据库拒绝:{str(exc).splitlines()[0]}")

    await dispose_engine()


if __name__ == "__main__":
    if os.environ.get("APP_ENV") == "test":
        raise SystemExit("演示脚本不要跑在测试库上")
    asyncio.run(main())
