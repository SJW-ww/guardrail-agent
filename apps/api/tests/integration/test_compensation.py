"""W6 补偿(Saga)验收。

补偿是「可回退」这句话闭环的地方,所以验收的重心不是「能跑通」,而是四条不变量:

1. **撤得干净**  已成功的写操作按逆序撤销,审计里能一条条对上;
2. **撤不干净就停手**  有一步没声明补偿动作,就一步都不撤(不做部分补偿);
3. **重复触发只撤一次**  补偿动作也走幂等账本,双击按钮不会关两张单;
4. **撤过之后不许重放**  补偿动过库的 run 不再接受 retry —— 先撤后重放,
   库里会多出一个「系统以为已经撤掉」的副作用。
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from guardrail_api.config import Settings
from guardrail_api.domain.errors import RuleViolation
from guardrail_api.governance.executor import PlannedStep, RunExecutor
from guardrail_api.models import AftersalesTicket, AgentRun, AuditLog, RunStatus, TicketStatus
from guardrail_api.services import aftersales as aftersales_service

from .test_governance import L4_SETTINGS, WORKER_ID, _paid_order, _seed_run

pytestmark = pytest.mark.integration

#: 失败后不自动补偿,把决定权交给调用方 —— 手动补偿那条路要单独验。
MANUAL = Settings(policy_actor_trust_levels="agent:guardrail=L4", compensation_mode="manual")
L4_AND_INTERN = Settings(
    policy_actor_trust_levels="agent:guardrail=L4,human:intern=L0", compensation_mode="manual"
)


@pytest.fixture
def factory(db_engine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


def _refund(
    seq: int,
    order_id: int,
    *,
    amount_cents: int = 100,
    description: str = "质量问题",
    **kwargs,
) -> PlannedStep:
    return PlannedStep(
        seq=seq,
        tool="create_refund",
        args={
            "order_id": order_id,
            "reason_code": "QUALITY_ISSUE",
            "amount_cents": amount_cents,
            "description": description,
        },
        **kwargs,
    )


def _broken_query(seq: int, *, depends_on: tuple[int, ...] = ()) -> PlannedStep:
    """必然失败的一步:拿它把计划打断在中间。"""
    return PlannedStep(
        seq=seq, tool="query_order", args={"order_no": "NOT-EXIST"}, depends_on=depends_on
    )


async def _run_plan(factory, run_uid: str, *, settings: Settings = L4_SETTINGS):
    executor = RunExecutor(factory, settings=settings, worker_id=WORKER_ID)
    assert await executor.claim(run_uid)
    result = await executor.execute_run(run_uid)
    return executor, result


async def _tickets(session) -> list[AftersalesTicket]:
    rows = await session.execute(select(AftersalesTicket).order_by(AftersalesTicket.id))
    return list(rows.scalars())


async def _audits(session) -> list[AuditLog]:
    return list((await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars())


async def _run(session, run_uid: str) -> AgentRun:
    session.expire_all()
    return (await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))).scalar_one()


# ---------------- 1. 撤得干净:逆序 ----------------


async def test_auto_compensation_rolls_back_in_reverse_order(session, factory) -> None:
    order = await _paid_order(session, order_no="SO2026010001")
    await session.commit()
    plan = [
        _refund(1, order.id, amount_cents=100),
        _refund(2, order.id, amount_cents=200),
        _broken_query(3),
    ]
    run_uid = await _seed_run(session, plan=plan)

    _, result = await _run_plan(factory, run_uid)
    assert result.status is RunStatus.COMPENSATED

    run = await _run(session, run_uid)
    assert run.status is RunStatus.COMPENSATED
    assert run.lease_owner is None, "补偿结束必须交还租约"
    assert run.checkpoint["compensation"]["status"] == "COMPENSATED"
    assert "NOT-EXIST" in (run.last_error or ""), "成功回滚也要留住「当初为什么失败」"

    tickets = await _tickets(session)
    assert [item.status for item in tickets] == [TicketStatus.CLOSED, TicketStatus.CLOSED]

    compensations = [row for row in await _audits(session) if row.policy_decision == "COMPENSATE"]
    assert [row.step_seq for row in compensations] == [-2, -1], "后做的先撤:补偿必须逆序"


# ---------------- 2. 没东西可撤,也必须落终态 ----------------


async def test_nothing_to_roll_back_still_finishes_and_releases_lease(session, factory) -> None:
    order = await _paid_order(session, order_no="SO2026010002")
    await session.commit()
    plan = [
        PlannedStep(seq=1, tool="query_order", args={"order_no": order.order_no}),
        _broken_query(2, depends_on=(1,)),
    ]
    run_uid = await _seed_run(session, plan=plan)

    executor, _ = await _run_plan(factory, run_uid)
    outcome = await executor.compensate(run_uid, actor="agent:guardrail", force=True)

    run = await _run(session, run_uid)
    assert outcome.status is RunStatus.FAILED, "撤回过 0 步,不能改成 COMPENSATED(那是谎称撤过)"
    assert run.status is RunStatus.FAILED, "没有可撤的写操作也必须落终态,不能卡在 RUNNING"
    assert run.lease_owner is None
    assert run.checkpoint["compensation"]["status"] == "NOTHING_TO_ROLLBACK"


# ---------------- 3. 撤不干净就一步都不撤 ----------------


async def test_blocked_compensation_does_not_partially_roll_back(session, factory) -> None:
    """第 2 步 close_ticket 没声明补偿动作 → 整条补偿计划放弃,第 1 步也不撤。"""
    order = await _paid_order(session, order_no="SO2026010003")
    await session.commit()
    plan = [
        _refund(1, order.id),
        PlannedStep(
            seq=2,
            tool="close_ticket",
            args={"ticket_id": {"$ref": "1.ticket_id"}, "reason": "客户撤回申请"},
            depends_on=(1,),
        ),
        _broken_query(3, depends_on=(2,)),
    ]
    run_uid = await _seed_run(session, plan=plan)

    _, result = await _run_plan(factory, run_uid)

    run = await _run(session, run_uid)
    assert result.status is RunStatus.FAILED
    assert run.status is RunStatus.FAILED
    assert run.checkpoint["compensation"]["status"] == "BLOCKED"
    assert "第 2 步" in (run.last_error or "") and "没有声明补偿动作" in (run.last_error or "")

    tickets = await _tickets(session)
    assert [item.status for item in tickets] == [TicketStatus.CLOSED], (
        "正向写留在库里(没撤),但绝不部分补偿"
    )
    assert not [row for row in await _audits(session) if row.policy_decision == "COMPENSATE"]


# ---------------- 4. 补偿本身也要过策略门 ----------------


async def test_compensation_needs_approval_then_force_executes(session, factory) -> None:
    """补偿动作 close_ticket 在 L2 下是「要人批」:自动补偿停手等人点,点了才撤。"""
    order = await _paid_order(session, order_no="SO2026010004")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])

    executor, _ = await _run_plan(factory, run_uid, settings=MANUAL)

    parked = await executor.compensate(run_uid, actor="human:operator-01", force=False)
    assert parked.status is RunStatus.FAILED
    run = await _run(session, run_uid)
    assert run.checkpoint["compensation"]["status"] == "NEEDS_APPROVAL"
    assert [item.status for item in await _tickets(session)] == [TicketStatus.PENDING]

    done = await executor.compensate(run_uid, actor="human:operator-01", force=True)
    assert done.status is RunStatus.COMPENSATED
    assert [item.status for item in await _tickets(session)] == [TicketStatus.CLOSED]


async def test_denied_compensation_keeps_the_writes(session, factory) -> None:
    """force 是「有人点头」,不是「绕过策略」:L0 执行体连补偿都不许做。"""
    order = await _paid_order(session, order_no="SO2026010005")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])

    executor, _ = await _run_plan(factory, run_uid, settings=L4_AND_INTERN)
    denied = await executor.compensate(run_uid, actor="human:intern", force=True)

    assert denied.status is RunStatus.FAILED
    assert any("策略拒绝" in item for item in denied.blockers)
    run = await _run(session, run_uid)
    assert run.checkpoint["compensation"]["status"] == "BLOCKED"
    assert [item.status for item in await _tickets(session)] == [TicketStatus.PENDING]
    assert not [row for row in await _audits(session) if row.policy_decision == "COMPENSATE"]


# ---------------- 5. 撤到一半失败:停手,并且不许再重放 ----------------


async def test_partial_compensation_blocks_retry(session, factory) -> None:
    order = await _paid_order(session, order_no="SO2026010006")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])

    executor, _ = await _run_plan(factory, run_uid, settings=MANUAL)

    # 别人抢先关了这张工单(CLOSED 是终态,再关一次会撞状态机)—— 补偿就会卡在这一步。
    ticket = (await _tickets(session))[0]
    await aftersales_service.close_ticket(session, ticket.id, operator="human:operator-09")
    await session.commit()

    outcome = await executor.compensate(run_uid, actor="agent:guardrail", force=True)
    assert outcome.status is RunStatus.FAILED
    run = await _run(session, run_uid)
    assert run.checkpoint["compensation"]["status"] == "PARTIAL"

    with pytest.raises(RuleViolation, match="补偿已经动过库"):
        await executor.retry_step(run_uid, 1)


# ---------------- 6. 重复触发只撤一次 ----------------


async def test_double_compensation_only_rolls_back_once(session, factory) -> None:
    """双击按钮 / 两个人同时点:只撤一次,另一个拿到明确的拒绝。"""
    order = await _paid_order(session, order_no="SO2026010007")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])
    executor, _ = await _run_plan(factory, run_uid, settings=MANUAL)

    results = await asyncio.gather(
        executor.compensate(run_uid, actor="agent:guardrail", force=True),
        executor.compensate(run_uid, actor="agent:guardrail", force=True),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RuleViolation) for item in results) == 1, results

    compensations = [row for row in await _audits(session) if row.policy_decision == "COMPENSATE"]
    assert len(compensations) == 1, "补偿动作也走幂等账本:只该有一条审计"
    assert [item.status for item in await _tickets(session)] == [TicketStatus.CLOSED]


# ---------------- 7. REST 入口:同一个按钮,点两次也只撤一次 ----------------


async def test_compensate_route_rolls_back_then_is_idempotent(session, factory, api_client) -> None:
    order = await _paid_order(session, order_no="SO2026010008")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])
    # 用 manual 跑出「失败但没自动撤」的现场,再交给 REST 入口去撤。
    await _run_plan(factory, run_uid, settings=MANUAL)

    response = await api_client.post(
        f"/api/runs/{run_uid}/compensate", headers={"X-Actor": "operator-01"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compensated"] == [-1], "撤掉的是第 1 步,用负数序号表示"
    assert body["blockers"] == []
    assert body["run"]["status"] == "COMPENSATED"
    assert body["run"]["checkpoint"]["compensation"]["status"] == "COMPENSATED"

    again = await api_client.post(
        f"/api/runs/{run_uid}/compensate", headers={"X-Actor": "operator-01"}
    )
    assert again.status_code == 200, again.text
    assert again.json()["compensated"] == [-1], "重复点击返回现状,不重复动库"

    compensations = [row for row in await _audits(session) if row.policy_decision == "COMPENSATE"]
    assert len(compensations) == 1
    assert [item.status for item in await _tickets(session)] == [TicketStatus.CLOSED]


async def test_compensate_route_refuses_a_run_that_is_not_failed(
    session, factory, api_client
) -> None:
    """还在等审批的 run 不能撤 —— 它没有「已成功的写操作」,撤也无从谈起。"""
    order = await _paid_order(session, order_no="SO2026010009")
    await session.commit()
    run_uid = await _seed_run(session, plan=[_refund(1, order.id), _broken_query(2)])

    response = await api_client.post(
        f"/api/runs/{run_uid}/compensate", headers={"X-Actor": "operator-01"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "rule_violation"
    assert "只允许对已失败的执行做补偿" in response.json()["error"]["message"]
