"""执行记录路由。

这里刻意只做三件事:**登记**一次执行、**查看**它的步骤、**内联推进**它。
真正的推进逻辑全在 `governance.executor` 里 —— 路由不是放业务规则的地方,
/api/runs/{uid}/execute 只是给演示和前端一个同步入口,
生产里同一个 run 由 worker 拉取执行,两条路径跑的是同一份执行器。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.api.deps import ActorDep, normalize_actor
from guardrail_api.config import get_settings
from guardrail_api.db import get_session, get_session_factory
from guardrail_api.domain.errors import PolicyDenied, RuleViolation
from guardrail_api.governance import policy, trace
from guardrail_api.governance.executor import PlannedStep, RunExecutor, create_run
from guardrail_api.governance.policy import PolicyDecision
from guardrail_api.models import AgentRun, AgentStep, RunStatus, StepKind, StepStatus
from guardrail_api.planner import draft
from guardrail_api.tools import load_tools
from guardrail_api.tools.registry import resolve_effective_amount

router = APIRouter(prefix="/api/runs", tags=["governance"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: 内联执行时使用的 worker 身份。它和执行进程抢的是同一套租约 ——
#: 所以「前端点一下」和「worker 拉取」不会同时推进同一个 run。
INLINE_WORKER_ID = "api-inline"


class PlannedStepIn(BaseModel):
    seq: int = Field(ge=1)
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False


class CreateRunRequest(BaseModel):
    goal: str = Field(min_length=2, max_length=500)
    steps: list[PlannedStepIn] = Field(default_factory=list)
    from_intent: bool = Field(
        default=False,
        description="忽略 steps,由规划器从 goal 生成一步计划(规则或 LLM,见 PLANNER_BACKEND)",
    )


class StepView(BaseModel):
    seq: int
    kind: StepKind
    status: StepStatus
    tool_name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    attempt: int
    replayed: bool
    idempotency_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunView(BaseModel):
    run_uid: str
    goal: str
    actor: str
    trace_id: str
    status: RunStatus
    plan: list[dict[str, Any]]
    checkpoint_seq: int
    checkpoint: dict[str, Any] | None = None
    waiting_ref: str | None = None
    # 兼容字段:由 approvals 派生。新代码请读 approvals,它才有"谁批的"
    approved_seqs: list[int]
    approvals: dict[str, list[str]] = Field(
        default_factory=dict,
        description="批准记录:{步骤序号: [批准人...]};大额操作需要多个不同角色的签名",
    )
    attempt: int
    last_error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class RunDetail(RunView):
    steps: list[StepView]


class RunListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[RunView]


class ExecuteResponse(BaseModel):
    run: RunDetail
    resumed_from: int | None = None
    executed: list[int] = Field(default_factory=list)
    replayed: list[int] = Field(default_factory=list)


def _to_view(run: AgentRun) -> RunView:
    return RunView(
        run_uid=run.run_uid,
        goal=run.goal,
        actor=run.actor,
        trace_id=run.trace_id,
        status=run.status,
        plan=list(run.plan),
        checkpoint_seq=run.checkpoint_seq,
        checkpoint=run.checkpoint,
        waiting_ref=run.waiting_ref,
        approved_seqs=list(run.approved_seqs or []),
        approvals=dict(run.approvals or {}),
        attempt=run.attempt,
        last_error=run.last_error,
        created_at=run.created_at,
        finished_at=run.finished_at,
    )


async def _detail(session: AsyncSession, run: AgentRun) -> RunDetail:
    rows = (
        (
            await session.execute(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
            )
        )
        .scalars()
        .all()
    )
    return RunDetail(
        **_to_view(run).model_dump(),
        steps=[StepView.model_validate(step, from_attributes=True) for step in rows],
    )


async def _by_uid(session: AsyncSession, run_uid: str) -> AgentRun:
    run = (
        await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
    ).scalar_one_or_none()
    if run is None:
        raise RuleViolation(f"执行记录 {run_uid} 不存在")
    return run


@router.post("", response_model=RunDetail, summary="登记一次执行(此时无任何副作用)")
async def create(body: CreateRunRequest, session: SessionDep, actor: ActorDep) -> RunDetail:
    if body.from_intent:
        proposal = await draft(session, body.goal, actor=actor)
        steps = [
            PlannedStep(
                seq=1,
                tool=proposal.action,
                args=proposal.arguments,
                requires_approval=proposal.requires_approval,
            )
        ]
    else:
        if not body.steps:
            raise RuleViolation("steps 为空:要么显式给出步骤,要么把 from_intent 置为 true")
        steps = [
            PlannedStep(
                seq=item.seq,
                tool=item.tool,
                args=item.args,
                requires_approval=item.requires_approval,
            )
            for item in body.steps
        ]

    run = await create_run(
        session,
        goal=body.goal,
        actor=actor,
        plan=steps,
        trace_id=trace.get_trace_id(),
    )
    await session.commit()
    return await _detail(session, run)


@router.get("", response_model=RunListResponse, summary="执行记录列表")
async def list_runs(
    session: SessionDep,
    status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    conditions = []
    if status is not None:
        conditions.append(AgentRun.status == status)

    total = await session.scalar(select(func.count()).select_from(AgentRun).where(*conditions))
    rows = (
        (
            await session.execute(
                select(AgentRun)
                .where(*conditions)
                .order_by(AgentRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return RunListResponse(
        total=int(total or 0), limit=limit, offset=offset, items=[_to_view(run) for run in rows]
    )


@router.get("/{run_uid}", response_model=RunDetail, summary="执行详情(含步骤与断点)")
async def get_run(run_uid: str, session: SessionDep) -> RunDetail:
    return await _detail(session, await _by_uid(session, run_uid))


@router.post(
    "/{run_uid}/execute", response_model=ExecuteResponse, summary="内联推进(与 worker 同一套执行器)"
)
async def execute_run(run_uid: str, session: SessionDep) -> ExecuteResponse:
    await _by_uid(session, run_uid)
    executor = RunExecutor(get_session_factory(), worker_id=INLINE_WORKER_ID)
    # 内联执行和 worker 抢的是同一把租约:不先领取就直接推进,等于给自己开例外
    if not await executor.claim(run_uid):
        raise RuleViolation(f"run {run_uid} 正被其他执行者持有,请稍后再试")
    result = await executor.execute_run(run_uid)

    session.expire_all()
    run = await _by_uid(session, run_uid)
    return ExecuteResponse(
        run=await _detail(session, run),
        resumed_from=result.resumed_from,
        executed=[o.seq for o in result.outcomes if not o.replayed],
        replayed=[o.seq for o in result.outcomes if o.replayed],
    )


@router.post("/{run_uid}/steps/{seq}/approve", response_model=RunDetail, summary="批准挂起的步骤")
async def approve_step(run_uid: str, seq: int, session: SessionDep, actor: ActorDep) -> RunDetail:
    """批准一个挂起的步骤。

    批准不是「点一下放行」,它是一次**有人签字**的事件,所以三件事都要做对:
    谁能批(策略裁决)、批了哪一步(白名单校验)、是谁批的(记名)。
    """
    run = await _by_uid(session, run_uid)
    step = next((item for item in run.plan if int(item["seq"]) == seq), None)
    if step is None:
        raise RuleViolation(f"步骤 {seq} 不在这次执行的计划里")

    settings = get_settings()
    spec = load_tools().get(str(step["tool"]))
    args = step.get("args") or {}
    # 要几个签字必须和执行器算出同一个数:金额不在参数里时,一样去问工具自己。
    resolved_amount = await resolve_effective_amount(spec, args, actor=run.actor, session=session)
    requirement = policy.approval_requirement(
        spec, args, settings=settings, resolved_amount=resolved_amount
    )
    existing = run.approvers_of(seq)

    # 已经签够了:重复请求按幂等处理,不报错也不重复计数
    if run.is_fully_approved(seq, requirement.required):
        return await _detail(session, run)

    approver = normalize_actor(actor)
    verdict = policy.evaluate_approval(
        run_actor=run.actor,
        approver=approver,
        tool=spec.name,
        existing_approvers=existing,
        required=requirement.required,
        settings=settings,
    )
    if verdict.decision is not PolicyDecision.ALLOW:
        raise PolicyDenied(
            verdict.reason,
            rule=verdict.rule,
            run_uid=run_uid,
            step_seq=seq,
            required=requirement.required,
            collected=existing,
        )

    # 记名。签名是**追加**的,不覆盖前面的人 —— 双人复核要的就是"不止一个人"。
    if approver not in existing:
        run.approvals = {**(run.approvals or {}), str(seq): [*existing, approver]}
    collected = run.approvers_of(seq)

    if len(collected) >= requirement.required:
        run.waiting_ref = None
        # 审批通过只是「允许继续」,不代表已经执行:状态回到 PENDING,等 worker 或调用方推进
        if run.status is RunStatus.WAITING_APPROVAL:
            run.status = RunStatus.PENDING
    else:
        # 还差人:状态保持等审批,但把进度更新到 checkpoint,界面上要看得见"还差几人"
        checkpoint = dict(run.checkpoint or {})
        checkpoint["approval"] = {
            "required": requirement.required,
            "collected": collected,
            "remaining": requirement.required - len(collected),
        }
        run.checkpoint = checkpoint

    await session.commit()
    session.expire_all()
    return await _detail(session, await _by_uid(session, run_uid))


class RetryStepResponse(BaseModel):
    run: RunDetail
    seq: int
    replayed: bool
    result: dict[str, Any] | None = None
    error: str | None = None


@router.post(
    "/{run_uid}/steps/{seq}/retry", response_model=RetryStepResponse, summary="重跑单步(幂等兜底)"
)
async def retry_step(run_uid: str, seq: int, session: SessionDep) -> RetryStepResponse:
    await _by_uid(session, run_uid)
    executor = RunExecutor(get_session_factory(), worker_id=INLINE_WORKER_ID)
    outcome = await executor.retry_step(run_uid, seq)

    session.expire_all()
    return RetryStepResponse(
        run=await _detail(session, await _by_uid(session, run_uid)),
        seq=outcome.seq,
        replayed=outcome.replayed,
        result=outcome.result,
        error=outcome.error,
    )
