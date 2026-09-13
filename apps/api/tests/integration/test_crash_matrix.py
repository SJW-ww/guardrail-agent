"""W7 崩溃矩阵:每一种「进程没了」的时机,不变量是否还成立。

矩阵本身见 README「崩溃矩阵(W7)」一节。四格里的两格由既有用例守着:

- 步骤间杀 → `test_governance.py::test_kill9_between_steps_resumes_without_repeating`
- 事务中杀 → `test_governance.py::test_kill9_inside_step_rolls_back_business_audit_and_idempotency`

这里补两格它们是空白的:**审批中杀** 和 **补偿中杀**。
两者都不是"业务写会不会重复"的问题,而是"这条 run 会不会被不该推进它的人推进"。
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from guardrail_api.config import Settings
from guardrail_api.governance import lease
from guardrail_api.governance.executor import RunExecutor
from guardrail_api.models import AgentRun, AuditLog, RunStatus, TicketStatus

from .test_compensation import MANUAL, _broken_query, _refund, _run_plan, _tickets
from .test_governance import _paid_order, _run_worker, _seed_run

pytestmark = pytest.mark.integration

#: 默认信任等级(L2)。高风险退款在这一档会挂起等人工审批 —— 这正是"审批中杀"要的场景。
L2 = Settings()


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


async def _run(session, run_uid: str) -> AgentRun:
    session.expire_all()
    return (await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))).scalar_one()


# ---------------- 审批中杀 ----------------


async def test_crash_while_waiting_for_approval_is_not_picked_up(session, factory) -> None:
    """等审批的 run 不能被「领一个待推进任务」的 worker 领走 —— 占着它等于把闸门拆了。"""
    order = await _paid_order(session, order_no="SO2026011001")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id)])

    crashed = RunExecutor(factory, settings=L2, worker_id="w-before-crash")
    assert await crashed.claim(run_uid)
    result = await crashed.execute_run(run_uid)
    assert result.status is RunStatus.WAITING_APPROVAL
    # 挂起时租约已经交还:等审批不是"在执行",不该占着别人的位置
    assert (await _run(session, run_uid)).lease_owner is None

    # 进程没了。新的 worker 来领任务:一个字都不许碰这条 run
    async with factory() as other:
        claimed = await lease.claim_runs(other, worker_id="w-after-crash", lease_seconds=30)
        assert [item.run_uid for item in claimed] == []
        await other.commit()

    # 批准之后才继续,而且业务表只写一次
    run = await _run(session, run_uid)
    run.approvals = {"1": ["human:supervisor-01"]}
    run.waiting_ref = None
    run.status = RunStatus.PENDING
    await session.commit()

    resumed = RunExecutor(factory, settings=L2, worker_id="w-after-approval")
    assert await resumed.claim(run_uid)
    assert (await resumed.execute_run(run_uid)).status is RunStatus.SUCCEEDED
    assert [item.status for item in await _tickets(session)] == [TicketStatus.PENDING]


# ---------------- 补偿中杀 ----------------


async def test_crash_mid_compensation_is_not_redriven_or_double_rolled_back(
    session, factory
) -> None:
    """补偿撤到一半被 SIGKILL:run 不能被当未完成任务领走,重新补偿也不能撤第二次。"""
    order = await _paid_order(session, order_no="SO2026011002")
    await session.commit()
    run_uid = await _seed_run(
        session, plan=[_refund(1, order.id), _broken_query(2, depends_on=(1,))]
    )
    # 用 manual 跑出「失败、但还没补偿」的现场
    await _run_plan(factory, run_uid, settings=MANUAL)
    assert (await _run(session, run_uid)).status is RunStatus.FAILED
    assert [item.status for item in await _tickets(session)] == [TicketStatus.PENDING]

    # 补偿进程在第 1 步补偿已提交、整轮还没收尾时被 SIGKILL(退出码 -9)
    code, output = await _run_worker(
        run_uid,
        worker_id="w-compensation",
        extra=["--compensate", "--crash-after-compensation", "1"],
        lease_seconds=2,
    )
    assert code == -9, output

    run = await _run(session, run_uid)
    assert run.status is RunStatus.FAILED, "补偿被 kill 后必须留在 FAILED,不能变成 RUNNING"
    assert [item.status for item in await _tickets(session)] == [TicketStatus.CLOSED]
    compensations = [
        row
        for row in (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars()
        if row.policy_decision == "COMPENSATE"
    ]
    assert len(compensations) == 1

    # 关键一格:哪怕租约已经过期,普通 worker 也领不走这条已经被撤掉一半的 run
    async with factory() as other:
        run = await _run(session, run_uid)
        run.lease_expires_at = run.lease_expires_at.replace(year=2000)
        await session.commit()
        claimed = await lease.claim_runs(other, worker_id="w-late", lease_seconds=30)
        assert [item.run_uid for item in claimed] == []
        await other.commit()

    # 重新补偿:命中的是幂等账本,不会关第二次单,最终收敛到 COMPENSATED
    again = RunExecutor(factory, settings=MANUAL, worker_id="w-compensation-2")
    outcome = await again.compensate(run_uid, actor="agent:guardrail", force=True)
    assert outcome.status is RunStatus.COMPENSATED
    rows = list((await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars())
    assert len([row for row in rows if row.policy_decision == "COMPENSATE"]) == 1, (
        "重放不该产生第二条补偿审计"
    )
    assert (await _run(session, run_uid)).status is RunStatus.COMPENSATED
