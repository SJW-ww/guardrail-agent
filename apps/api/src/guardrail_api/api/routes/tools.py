"""工具网关路由:暴露工具声明,并提供唯一的安全调用入口。

只读工具直接执行;**写工具一律转成一次 run,由执行器推进** ——
幂等账本、审计、checkpoint 一个都不能少。UI 点「执行」和 Agent 自动执行
走的是同一条路径,不存在「前端这条腿绕过治理」的口子。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.api.deps import ActorDep
from guardrail_api.db import get_session, get_session_factory
from guardrail_api.domain.errors import RuleViolation
from guardrail_api.governance import trace
from guardrail_api.governance.executor import PlannedStep, RunExecutor, create_run
from guardrail_api.models import AgentRun, RunStatus, StepStatus
from guardrail_api.tools import ToolContext, load_tools, registry

router = APIRouter(prefix="/api/tools", tags=["tools"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ToolDescription(BaseModel):
    name: str
    title: str
    description: str
    risk_level: str
    read_only: bool
    parameters: dict[str, Any]
    preconditions: list[str]
    side_effect: str | None = None
    idempotent: bool
    idempotency_key: str | None = None
    compensate_tool: str | None = None
    tags: list[str]


class InvokeToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationResponse(BaseModel):
    tool: str
    actor: str
    risk_level: str
    read_only: bool
    side_effect: str | None = None
    result: dict[str, Any]
    # 写操作会顺带创建一条 run,便于在「执行与审计」里追溯
    run_uid: str | None = None
    status: RunStatus | None = None
    message: str | None = None


INLINE_WORKER_ID = "api-tool-gateway"


@router.get("", response_model=list[ToolDescription], summary="工具清单与声明")
async def list_tools() -> list[ToolDescription]:
    return [ToolDescription(**item) for item in load_tools().describe()]


@router.post("/{tool_name}/invoke", response_model=ToolInvocationResponse, summary="调用工具")
async def invoke_tool(
    tool_name: str,
    body: InvokeToolRequest,
    response: Response,
    session: SessionDep,
    actor: ActorDep,
) -> ToolInvocationResponse:
    """所有写操作都必须走这里 —— 编排层、Agent、UI 共用同一条受治理的路径。

    W1 的调用者是「人点执行」(L1 建议模式),所以 actor 前缀是 human;
    W3 接入 Agent 后,Agent 会用 agent: 前缀走同一个入口。
    """
    load_tools()
    spec = registry.get(tool_name)
    context = ToolContext(session=session, actor=f"human:{actor}")

    if spec.is_read_only:
        # 只读不进治理链路:它不改库,写审计只会淹没真正的写操作
        result = await registry.invoke(tool_name, context, body.arguments)
        await session.commit()
        return ToolInvocationResponse(
            tool=tool_name,
            actor=context.actor,
            risk_level=spec.risk_level.value,
            read_only=True,
            side_effect=None,
            result=result.model_dump(mode="json"),
        )

    run = await create_run(
        session,
        goal=f"人点执行:{spec.title}",
        actor=context.actor,
        plan=[PlannedStep(seq=1, tool=tool_name, args=body.arguments)],
        trace_id=trace.get_trace_id(),
    )
    await session.commit()

    executor = RunExecutor(get_session_factory(), worker_id=INLINE_WORKER_ID)
    if not await executor.claim(run.run_uid):
        raise RuleViolation(f"run {run.run_uid} 正被其他执行者持有,请稍后再试")
    execution = await executor.execute_run(run.run_uid)

    outcome = execution.outcomes[0] if execution.outcomes else None

    # 策略层判了「要人批」:网关没有权力替审批人做决定,也不该假装这是一次失败。
    # 如实返回 202 + run_uid,调用方去审批中心接着走。
    if execution.status is RunStatus.WAITING_APPROVAL:
        # expire_all() 之后不要再去碰 run 的任何属性 —— 那会触发一次同步懒加载,
        # 在 async 上下文里直接炸 MissingGreenlet。主键先取出来。
        run_uid = run.run_uid
        session.expire_all()
        fresh = await _by_uid(session, run_uid)
        policy_info = (fresh.checkpoint or {}).get("policy") or {}
        response.status_code = status.HTTP_202_ACCEPTED
        return ToolInvocationResponse(
            tool=tool_name,
            actor=context.actor,
            risk_level=spec.risk_level.value,
            read_only=False,
            side_effect=spec.side_effect,
            result={},
            run_uid=run_uid,
            status=fresh.status,
            message=(
                f"该操作需人工审批,已登记执行记录 {run_uid}。"
                f"原因:{policy_info.get('reason', '策略要求人工审批')}"
            ),
        )

    if outcome is None or outcome.status is not StepStatus.SUCCEEDED:
        # 领域错误原样抛出:HTTP 层才能给出字段级错误,而不是一句笼统的失败
        if outcome is not None and outcome.cause is not None:
            raise outcome.cause
        raise RuleViolation(outcome.error if outcome else "执行没有产生结果")

    return ToolInvocationResponse(
        tool=tool_name,
        actor=context.actor,
        risk_level=spec.risk_level.value,
        read_only=False,
        side_effect=spec.side_effect,
        result=outcome.result or {},
        run_uid=run.run_uid,
        status=execution.status,
    )


async def _by_uid(session: AsyncSession, run_uid: str) -> AgentRun:
    run = await session.scalar(select(AgentRun).where(AgentRun.run_uid == run_uid))
    if run is None:
        raise RuleViolation(f"执行记录 {run_uid} 不存在")
    return run
