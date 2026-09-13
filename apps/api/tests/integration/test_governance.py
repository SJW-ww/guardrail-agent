"""W2 治理内核验收。

四条不变量,每一条都有对应的失败注入 —— 不是「读一遍代码觉得对」:

1. 幂等      同一 key 重放 10 次 / 并发提交 2 次,副作用只发生一次
2. 续跑      进程在步骤之间被 SIGKILL,重启后从 checkpoint 继续,不重放已完成步骤
3. 不半途    进程在事务中途被 SIGKILL,业务写 / 审计写 / 幂等写一起回滚
4. 租约      同一 run 不会被两个 worker 同时领取;持有者死了租约到期可被接管
5. 审计      任意一次写都能查出完整 before / after,且改不掉
"""

import asyncio
import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from guardrail_api.config import Settings
from guardrail_api.domain.clock import utcnow
from guardrail_api.governance import lease
from guardrail_api.governance.audit_ddl import AUDIT_GUARD_UPGRADE
from guardrail_api.governance.executor import PlannedStep, RunExecutor, create_run
from guardrail_api.models import (
    AftersalesTicket,
    AgentRun,
    AgentStep,
    AuditLog,
    Customer,
    IdempotencyRecord,
    Inventory,
    Order,
    Product,
    RunStatus,
    StepStatus,
)
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine

pytestmark = pytest.mark.integration

ADDRESS = Address(receiver_name="张三", receiver_phone="13800000000", address="深圳市南山区 1 号")
WORKER_ID = "test-worker"

# 这一组用例验证的是幂等 / 续跑 / 审计这些**机制本身**,需要一个能自动执行高风险步骤的
# 执行体。接上策略引擎之后,「能不能自动执行」不再由计划里的布尔值决定,而是由执行体的
# 信任等级决定 —— 所以这里显式授权 L4,而不是把用例改成期望「卡在审批」。
L4_SETTINGS = Settings(policy_actor_trust_levels="agent:guardrail=L4")


# ---------------- 夹具 ----------------


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def audit_guard(db_engine):
    """给测试库补上迁移里的 append-only 触发器(create_all 不带触发器)。"""
    async with db_engine.begin() as connection:
        for statement in AUDIT_GUARD_UPGRADE:
            await connection.execute(text(statement))
    return db_engine


async def _paid_order(session: AsyncSession, *, order_no: str, price_cents: int = 10_000) -> Order:
    customer = Customer(name="张三", email=f"{order_no.lower()}@example.com", phone="13800000000")
    product = Product(
        sku=f"SKU-{order_no}", name="测试商品", category="demo", price_cents=price_cents
    )
    session.add_all([customer, product])
    await session.flush()
    session.add(Inventory(product_id=product.id, available_qty=10, reserved_qty=0))
    await session.flush()

    order = await order_service.create_order(
        session,
        customer_id=customer.id,
        lines=[OrderLine(product_id=product.id, quantity=2)],
        address=ADDRESS,
        order_no=order_no,
    )
    await order_service.pay_order(session, order.id)
    return order


async def _seed_run(
    session: AsyncSession, *, plan: list[PlannedStep], goal: str = "治理层验收"
) -> str:
    run = await create_run(
        session, goal=goal, actor="agent:guardrail", plan=plan, trace_id="trace-test"
    )
    await session.commit()
    return run.run_uid


def _refund_step(seq: int, order_id: int, *, description: str = "质量问题") -> PlannedStep:
    return PlannedStep(
        seq=seq,
        tool="create_refund",
        args={
            "order_id": order_id,
            "reason_code": "QUALITY_ISSUE",
            "amount_cents": 100,
            "description": description,
        },
    )


async def _ticket_count(session: AsyncSession, order_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AftersalesTicket)
            .where(AftersalesTicket.order_id == order_id)
        )
        or 0
    )


# ---------------- 1. 幂等 ----------------


async def test_replay_ten_times_produces_one_side_effect(session: AsyncSession, factory) -> None:
    """同一 run 的同一步骤重放 10 次:业务表只多一条,审计只多一行。"""
    order = await _paid_order(session, order_no="SO2026001001")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    executor = RunExecutor(factory, settings=L4_SETTINGS, worker_id=WORKER_ID)
    outcomes = [await executor.retry_step(run_uid, 1) for _ in range(10)]

    assert all(outcome.status is StepStatus.SUCCEEDED for outcome in outcomes)
    assert outcomes[0].replayed is False, "第一次是真执行"
    assert all(outcome.replayed for outcome in outcomes[1:]), "后九次全部命中幂等账本"
    assert {outcome.result["ticket_no"] for outcome in outcomes} == {
        outcomes[0].result["ticket_no"]
    }

    assert await _ticket_count(session, order.id) == 1
    records = (await session.execute(select(IdempotencyRecord))).scalars().all()
    assert len(records) == 1
    audits = (await session.execute(select(AuditLog))).scalars().all()
    assert len(audits) == 1, "重放不写审计:审计行代表世界上真的发生过一次写"


async def test_concurrent_same_key_only_one_wins(session: AsyncSession, factory) -> None:
    """两个协程同时提交同一 key:唯一索引兜底,只有一个真正执行。"""
    order = await _paid_order(session, order_no="SO2026001002")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    first, second = await asyncio.gather(
        RunExecutor(factory, settings=L4_SETTINGS, worker_id="w-a").retry_step(run_uid, 1),
        RunExecutor(factory, settings=L4_SETTINGS, worker_id="w-b").retry_step(run_uid, 1),
    )

    assert first.result["ticket_no"] == second.result["ticket_no"], "两边拿到同一张工单"
    assert sorted([first.replayed, second.replayed]) == [False, True], "一真一重放"
    assert await _ticket_count(session, order.id) == 1


# ---------------- 2 / 3. 崩溃恢复 ----------------


async def _run_worker(
    run_uid: str, *, worker_id: str, extra: list[str], lease_seconds: int = 2
) -> tuple[int, str]:
    """把 worker 作为独立进程拉起。返回 (退出码, 输出) —— SIGKILL 的退出码是 -9。"""
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "APP_ENV": "test",
        "LEASE_SECONDS": str(lease_seconds),
        "HEARTBEAT_INTERVAL_SECONDS": "1",
        "POLICY_ACTOR_TRUST_LEVELS": "agent:guardrail=L4",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "guardrail_api.governance.worker",
        "--run",
        run_uid,
        "--once",
        "--worker-id",
        worker_id,
        *extra,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    return process.returncode or 0, (stdout or b"").decode(errors="replace")


async def _resume_until_settled(
    factory, run_uid: str, *, worker_id: str, attempts: int = 6, lease_seconds: int = 2
) -> None:
    """反复拉起 worker 直到 run 进入终态。

    这里刻意不等一个固定时长再断言:租约到期是个时间竞态,
    真实 worker 本来也是靠轮询接手。等「租约一定过期」的写法会在负载高时假失败。
    """
    for _ in range(attempts):
        await asyncio.sleep(lease_seconds + 0.5)
        code, output = await _run_worker(run_uid, worker_id=worker_id, extra=[])
        assert code == 0, output
        async with factory() as check:
            run = (
                await check.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
            ).scalar_one()
            if run.status.is_terminal:
                return
    raise AssertionError("租约到期后 run 仍未被重新领取")


async def test_kill9_between_steps_resumes_without_repeating(
    session: AsyncSession, factory
) -> None:
    """在第 3 步之前 SIGKILL:重启后从 checkpoint 继续,1-2 步不重放。"""
    first = await _paid_order(session, order_no="SO2026002001")
    second = await _paid_order(session, order_no="SO2026002002")
    # 只留主键:后面会 expire_all 刷新子进程写进来的状态,
    # 那时再碰 ORM 对象的属性会触发一次同步懒加载(直接炸 MissingGreenlet)。
    first_id, second_id = first.id, second.id
    await session.commit()

    run_uid = await _seed_run(
        session,
        plan=[
            PlannedStep(seq=1, tool="query_order", args={"order_id": first_id}),
            _refund_step(2, first_id, description="第一步退款"),
            _refund_step(3, second_id, description="第二步退款"),
        ],
    )

    code, output = await _run_worker(run_uid, worker_id="w1", extra=["--crash-before-step", "3"])
    assert code == -9, f"应该真的被 SIGKILL,实际退出码 {code}\n{output}"

    steps = (await session.execute(select(AgentStep).order_by(AgentStep.seq))).scalars().all()
    assert [(step.seq, step.status) for step in steps] == [
        (1, StepStatus.SUCCEEDED),
        (2, StepStatus.SUCCEEDED),
    ]
    assert await _ticket_count(session, first_id) == 1, "第 2 步的写入已提交"
    assert await _ticket_count(session, second_id) == 0, "第 3 步还没跑"

    # 租约到期后由新 worker 接管 —— 等价于真实场景里等了 30 秒
    await _resume_until_settled(factory, run_uid, worker_id="w2")

    session.expire_all()
    steps = (await session.execute(select(AgentStep).order_by(AgentStep.seq))).scalars().all()
    assert [step.status for step in steps] == [StepStatus.SUCCEEDED] * 3
    assert [step.attempt for step in steps] == [1, 1, 1], "已完成的步骤没有被重跑"
    assert await _ticket_count(session, first_id) == 1
    assert await _ticket_count(session, second_id) == 1

    run = (await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))).scalar_one()
    assert run.status is RunStatus.SUCCEEDED
    assert run.checkpoint_seq == 3
    assert run.checkpoint is not None and run.checkpoint["completed"] == [1, 2, 3]


async def test_kill9_inside_step_rolls_back_business_audit_and_idempotency(
    session: AsyncSession, factory
) -> None:
    """在第 2 步的事务中途 SIGKILL:业务写 / 审计写 / 幂等写必须一起消失。"""
    order = await _paid_order(session, order_no="SO2026002003")
    order_id = order.id
    await session.commit()

    run_uid = await _seed_run(
        session,
        plan=[
            PlannedStep(seq=1, tool="query_order", args={"order_id": order_id}),
            _refund_step(2, order_id),
        ],
    )

    code, output = await _run_worker(run_uid, worker_id="w1", extra=["--crash-during-step", "2"])
    assert code == -9, f"应该真的被 SIGKILL,实际退出码 {code}\n{output}"

    assert await _ticket_count(session, order_id) == 0, "业务写随事务回滚"
    assert len((await session.execute(select(AuditLog))).scalars().all()) == 0, "审计写随事务回滚"
    assert len((await session.execute(select(IdempotencyRecord))).scalars().all()) == 0, (
        "幂等占位随事务回滚 —— 否则下一步会误判成『已经执行过』"
    )
    seqs = (await session.execute(select(AgentStep.seq))).scalars().all()
    assert list(seqs) == [1], "崩溃那一步没有留下任何痕迹"

    await _resume_until_settled(factory, run_uid, worker_id="w2")

    session.expire_all()
    assert await _ticket_count(session, order_id) == 1
    assert len((await session.execute(select(AuditLog))).scalars().all()) == 1


# ---------------- 4. 租约 ----------------


async def test_retrying_an_earlier_step_does_not_rewind_checkpoint(
    session: AsyncSession, factory
) -> None:
    """回到第 1 步重放:checkpoint 不回退,已完成的 run 不被改回「执行中」。

    这两条都不显眼,但破坏了它们,恢复逻辑就会从已经做完的地方重来。
    """
    order = await _paid_order(session, order_no="SO2026003003")
    order_id = order.id
    await session.commit()

    run_uid = await _seed_run(
        session,
        plan=[
            PlannedStep(seq=1, tool="query_order", args={"order_id": order_id}),
            _refund_step(2, order_id),
        ],
    )

    executor = RunExecutor(factory, settings=L4_SETTINGS, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    await executor.execute_run(run_uid)

    replayed = await executor.retry_step(run_uid, 1)
    assert replayed.replayed is False, "只读步骤不进幂等账本,每次都真的查一次"

    session.expire_all()
    run = (await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))).scalar_one()
    assert run.checkpoint_seq == 2, "重放第 1 步不能把断点拽回 1"
    assert run.status is RunStatus.SUCCEEDED, "已完成的执行不该被一次重放改成执行中"
    assert await _ticket_count(session, order_id) == 1


async def test_lease_prevents_double_claim_and_allows_takeover(
    session: AsyncSession, factory
) -> None:
    order = await _paid_order(session, order_no="SO2026003001")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    async with factory() as holder, factory() as rival:
        claimed = await lease.claim_runs(holder, worker_id="w1", lease_seconds=30, run_uid=run_uid)
        # 持有者还没提交,rival 靠 SKIP LOCKED 直接跳过而不是排队等锁
        blocked = await lease.claim_runs(rival, worker_id="w2", lease_seconds=30)
        await holder.commit()
        await rival.commit()

    assert [run.run_uid for run in claimed] == [run_uid]
    assert blocked == [], "同一个 run 不会被第二个 worker 领取"

    # 模拟租约到期(进程被杀、又没有别人帮忙释放的那种)
    async with factory() as session2:
        await session2.execute(
            update(AgentRun)
            .where(AgentRun.run_uid == run_uid)
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
        await session2.commit()

    async with factory() as session3:
        taken = await lease.claim_runs(session3, worker_id="w2", lease_seconds=30, run_uid=run_uid)
        await session3.commit()

    assert [run.run_uid for run in taken] == [run_uid]
    assert taken[0].lease_owner == "w2", "死掉的持有者留下的租约到期后可以被接管"
    assert taken[0].attempt == 2, "接管会累加 attempt,便于观测重试次数"


async def test_heartbeat_rejected_after_lease_taken_over(session: AsyncSession, factory) -> None:
    """旧持有者续约必须失败 —— 它不能再往一个已经不属于自己的 run 上写。"""
    order = await _paid_order(session, order_no="SO2026003002")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    async with factory() as session2:
        await lease.claim_runs(session2, worker_id="w1", lease_seconds=30, run_uid=run_uid)
        await session2.commit()

    async with factory() as session3:
        assert await lease.heartbeat(session3, run_uid=run_uid, worker_id="w1", lease_seconds=30)
        assert not await lease.heartbeat(
            session3, run_uid=run_uid, worker_id="w2", lease_seconds=30
        )
        await session3.commit()


# ---------------- 5. 审计 ----------------


async def test_audit_records_before_after_and_is_append_only(
    session: AsyncSession, factory, audit_guard
) -> None:
    order = await _paid_order(session, order_no="SO2026004001")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id, description="运输破损")])

    executor = RunExecutor(factory, settings=L4_SETTINGS, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    await executor.execute_run(run_uid)

    session.expire_all()
    entry = (await session.execute(select(AuditLog))).scalar_one()

    assert entry.actor == "agent:guardrail"
    assert entry.trace_id == "trace-test"
    assert entry.run_uid == run_uid
    assert entry.step_seq == 1
    assert entry.outcome.value == "SUCCEEDED"
    assert entry.reason == "运输破损"

    # before 只有订单、没有工单;after 多出一条 PENDING 工单 —— 这就是可读的 diff
    assert entry.before["tickets"] == []
    assert [t["status"] for t in entry.after["tickets"]] == ["PENDING"]
    assert entry.before["order"]["status"] == "PAID"

    with pytest.raises(DBAPIError) as excinfo:
        await session.execute(text("UPDATE audit_log SET reason = '改一下'"))
    assert "追加写" in str(excinfo.value)

    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM audit_log"))


# ---------------- 6. 策略裁决 ----------------


async def _run_of(session: AsyncSession, run_uid: str) -> AgentRun:
    return (await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))).scalar_one()


async def test_default_level_escalates_high_risk_and_blocks_before_writing(
    session: AsyncSession, factory
) -> None:
    """默认信任等级(未授权任何 actor)下,高风险退款必须先审批。

    关键断言不是「状态是等审批」,而是**审批之前业务表一行都没多** ——
    护栏要是只在状态机上有,那它就是个装饰。
    """
    order = await _paid_order(session, order_no="SO2026005001")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    executor = RunExecutor(factory, worker_id=WORKER_ID)  # 不带 L4 授权
    assert await executor.claim(run_uid)
    result = await executor.execute_run(run_uid)

    assert result.status is RunStatus.WAITING_APPROVAL
    assert await _ticket_count(session, order.id) == 0, "没审批之前不许写业务表"

    session.expire_all()
    run = await _run_of(session, run_uid)
    assert run.checkpoint is not None
    assert run.checkpoint["policy"]["decision"] == "REQUIRE_APPROVAL"
    assert run.checkpoint["policy"]["rule"] == "high_risk_requires_approval"
    assert "必须人工审批" in run.checkpoint["policy"]["reason"]
    assert run.approved_seqs == []
    assert run.approvals == {}


async def test_approved_step_resumes_and_executes(session: AsyncSession, factory) -> None:
    """批准之后同一个 run 继续跑完 —— 裁决是 REQUIRE_APPROVAL 不等于永远不能执行。"""
    order = await _paid_order(session, order_no="SO2026005002")
    order_id = order.id
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order_id)])

    executor = RunExecutor(factory, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    await executor.execute_run(run_uid)

    session.expire_all()
    run = await _run_of(session, run_uid)
    assert run.lease_owner is None, "等审批时不该占着租约,否则审批通过后没人能推进它"
    assert run.approvals == {}, "还没人批过"
    # 批准要记名:留下的是"谁签的字",不是一个孤零零的序号
    run.approvals = {"1": "supervisor-01"}
    run.status = RunStatus.PENDING
    await session.commit()

    # 批准之后重新领取 —— 这正是真实链路里 worker / 内联执行走的动作
    assert await executor.claim(run_uid)
    result = await executor.execute_run(run_uid)

    assert result.status is RunStatus.SUCCEEDED
    assert await _ticket_count(session, order_id) == 1

    # 审计不仅要记下"策略要求审批",还要记下"谁批的" —— 光有"被批过"答不出责任在谁
    session.expire_all()
    entry = (await session.execute(select(AuditLog))).scalar_one()
    assert entry.policy_decision == "REQUIRE_APPROVAL"
    assert "已获 supervisor-01 批准后执行" in (entry.policy_reason or "")


async def test_l0_executor_is_denied_and_the_denial_is_audited(
    session: AsyncSession, factory
) -> None:
    """只读档的执行体想写:拒绝 + 留痕。被拒绝的尝试也是事实,不能只在日志里。"""
    order = await _paid_order(session, order_no="SO2026005003")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    read_only = Settings(policy_actor_trust_levels="agent:guardrail=L0")
    executor = RunExecutor(factory, settings=read_only, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    result = await executor.execute_run(run_uid)

    assert result.status is RunStatus.FAILED
    assert await _ticket_count(session, order.id) == 0

    session.expire_all()
    run = await _run_of(session, run_uid)
    assert run.last_error is not None
    assert "策略引擎拒绝执行" in run.last_error

    entry = (await session.execute(select(AuditLog))).scalar_one()
    assert entry.outcome.value == "FAILED"
    assert entry.policy_decision == "DENY"
    assert "L0" in (entry.policy_reason or "")


async def test_executed_step_audit_carries_the_allow_verdict(
    session: AsyncSession, factory
) -> None:
    """放行也要留理由:L4 为什么能自动执行,审计行里要答得出来。"""
    order = await _paid_order(session, order_no="SO2026005004")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    executor = RunExecutor(factory, settings=L4_SETTINGS, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    await executor.execute_run(run_uid)

    session.expire_all()
    entry = (await session.execute(select(AuditLog))).scalar_one()

    assert entry.policy_decision == "ALLOW"
    assert entry.policy_reason is not None
    assert "L4" in entry.policy_reason
    assert "额度" in entry.policy_reason


async def test_retry_cannot_open_a_denied_step(session: AsyncSession, factory) -> None:
    """人工重试不等于人工越权:DENY 的步骤点几次重试都还是拒绝。"""
    order = await _paid_order(session, order_no="SO2026005005")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund_step(1, order.id)])

    read_only = Settings(policy_actor_trust_levels="agent:guardrail=L0")
    executor = RunExecutor(factory, settings=read_only, worker_id=WORKER_ID)
    outcome = await executor.retry_step(run_uid, 1)

    assert outcome.status is StepStatus.FAILED
    assert "策略引擎拒绝执行" in (outcome.error or "")
    assert await _ticket_count(session, order.id) == 0
