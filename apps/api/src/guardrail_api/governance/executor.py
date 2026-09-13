"""执行器:把「计划」变成可恢复、不重复、可审计的写操作。

三条不变量,任何一条被破坏都视为线上事故:

1. **不重复** —— 每个写步骤先抢幂等账本的主键。抢不到就复用结果,不再碰业务表。
2. **不半途** —— 业务写、审计写、幂等终态、步骤状态**同一次提交**。
   事务回滚时它们一起消失,所以「步骤 SUCCEEDED」永远不会是假的。
3. **可续跑** —— 每完成一步就把 checkpoint 落库。进程被 kill -9 后,
   重启的 worker 从「最大的已成功 seq」之后接着跑,前面的步骤不重放。

checkpoint 在这里是**被读的**:`_completed_seqs()` 从 `agent_step` 把已成功的
步骤捞回来,决定从哪里开始。这和「写个 JSON 字段给人看」是两回事。
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from guardrail_api.config import Settings, get_settings
from guardrail_api.domain.clock import utcnow
from guardrail_api.domain.errors import (
    DomainError,
    LeaseLost,
    PlanError,
    RuleViolation,
    RunBudgetExceeded,
)
from guardrail_api.governance import audit, compensation, idempotency, lease, policy
from guardrail_api.governance import plan as plan_module
from guardrail_api.governance.policy import PolicyDecision, PolicyVerdict
from guardrail_api.models import (
    AgentRun,
    AgentStep,
    AuditOutcome,
    IdempotencyStatus,
    RunStatus,
    StepKind,
    StepStatus,
)
from guardrail_api.tools.base import ToolContext, ToolSpec
from guardrail_api.tools.registry import ToolRegistry, load_tools, resolve_effective_amount

logger = logging.getLogger("guardrail.executor")

# 混沌钩子:用于演示/测试「进程被杀」。事件名 + step seq。
# 生产环境永远是 None —— 但它换来的是「崩溃恢复」这条路径可以被自动化验证。
ChaosHook = Callable[[str, int], None]

CHAOS_BEFORE_STEP_TXN = "before_step_txn"
CHAOS_AFTER_TOOL_WRITE = "after_tool_write"
#: 一条补偿动作已经提交、但整轮补偿还没收尾。用来验证「补偿撤到一半被 kill」
#: 之后:run 不会被普通 worker 领走,重新补偿也不会撤第二次。
CHAOS_AFTER_COMPENSATION_COMMIT = "after_compensation_commit"


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """计划里的一步。刻意只用 JSON 可表达的结构,方便整条计划落库与回放。

    `depends_on` 声明「这一步必须等哪些步骤成功」;`args` 里可以写
    `{"$ref": "1.ticket_id"}` 引用前面步骤的产出。两条规则都由
    `governance.plan` 在登记时校验、在推进时解析。
    """

    seq: int
    tool: str
    args: dict[str, Any]
    requires_approval: bool = False
    kind: StepKind = StepKind.EXECUTE
    depends_on: tuple[int, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlannedStep":
        return cls(
            seq=int(raw["seq"]),
            tool=str(raw["tool"]),
            args=dict(raw.get("args") or {}),
            requires_approval=bool(raw.get("requires_approval", False)),
            kind=StepKind(raw.get("kind", StepKind.EXECUTE.value)),
            depends_on=tuple(int(item) for item in raw.get("depends_on") or ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "args": self.args,
            "requires_approval": self.requires_approval,
            "kind": self.kind.value,
            "depends_on": list(self.depends_on),
        }

    def with_args(self, args: dict[str, Any]) -> "PlannedStep":
        """换掉参数,其余不变 —— 解析完引用之后仍然是一个完整的步骤。"""
        return PlannedStep(
            seq=self.seq,
            tool=self.tool,
            args=args,
            requires_approval=self.requires_approval,
            kind=self.kind,
            depends_on=self.depends_on,
        )


@dataclass(frozen=True, slots=True)
class StepOutcome:
    seq: int
    tool: str
    status: StepStatus
    replayed: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    idempotency_key: str | None = None
    # 原始异常对象。领域错误带字段级 context,调用方(HTTP 入口)需要它才能给出
    # 「哪个字段错了」而不是一句笼统的失败理由。
    cause: Exception | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class CompensationResult:
    """一次补偿的结果。`blockers` 非空说明:**部分或全部写操作还留在库里**,
    而且原因写清楚了 —— 这是给人和 runbook 看的,不是给日志看的。
    """

    status: RunStatus
    outcomes: list[StepOutcome] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def compensated(self) -> list[StepOutcome]:
        return [item for item in self.outcomes if item.status is StepStatus.SUCCEEDED]


@dataclass(slots=True)
class ExecutionResult:
    run_uid: str
    status: RunStatus
    outcomes: list[StepOutcome] = field(default_factory=list)
    # 本次是从哪一步开始跑的。等于计划第一步说明是全新执行;更大说明发生了续跑。
    resumed_from: int | None = None

    @property
    def replayed(self) -> list[StepOutcome]:
        return [outcome for outcome in self.outcomes if outcome.replayed]


@dataclass(slots=True)
class _Heartbeat:
    """心跳状态。放在这里而不是 executor 上,是为了让 executor 可以被并发复用。"""

    lost: bool = False
    error: str | None = None


async def create_run(
    session: AsyncSession,
    *,
    goal: str,
    actor: str,
    plan: list[PlannedStep],
    trace_id: str | None = None,
    step_budget: int = 20,
    token_budget: int = 8000,
) -> AgentRun:
    """登记一次执行。此时还没有任何副作用 —— 只是把「打算做什么」写下来。"""
    # 计划不合法就不该产生 run:步骤号乱序、依赖指向不存在的步骤、引用写错路径,
    # 全部在这里挡住 —— 而不是跑了两步才发现第三步依赖的东西永远不会来。
    plan_module.validate(plan, registry=load_tools())

    run = AgentRun(
        run_uid=str(uuid.uuid4()),
        goal=goal,
        actor=actor,
        trace_id=trace_id or uuid.uuid4().hex,
        status=RunStatus.PENDING,
        plan=[step.to_dict() for step in sorted(plan, key=lambda item: item.seq)],
        step_budget=step_budget,
        token_budget=token_budget,
    )
    session.add(run)
    await session.flush()
    return run


class RunExecutor:
    """推进一次 run,直到它成功、失败或卡在审批上。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
        chaos: ChaosHook | None = None,
        heartbeat_interval: int | None = None,
    ) -> None:
        self._sessions = session_factory
        self.worker_id = worker_id
        self.registry = registry or load_tools()
        self.settings = settings or get_settings()
        self.chaos = chaos
        self.heartbeat_interval = heartbeat_interval or self.settings.heartbeat_interval_seconds

    # ---------- 对外入口 ----------

    async def execute_run(self, run_uid: str) -> ExecutionResult:
        await self._assert_holds_lease(run_uid)
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            plan = [PlannedStep.from_dict(raw) for raw in run.plan]
            run_id = run.id

        completed = await self._completed_seqs(run_id)
        resumed_from = next((step.seq for step in plan if step.seq not in completed), None)
        result = ExecutionResult(
            run_uid=run_uid, status=RunStatus.RUNNING, resumed_from=resumed_from
        )

        heartbeat = _Heartbeat()
        beater = asyncio.create_task(self._heartbeat_loop(run_uid, heartbeat))
        try:
            await self._drive(run_uid, plan, result, heartbeat)
        finally:
            beater.cancel()
            await asyncio.gather(beater, return_exceptions=True)

        return result

    async def claim(
        self,
        run_uid: str,
        *,
        statuses: Sequence[RunStatus] | None = None,
        count_attempt: bool = True,
        mark_running: bool = True,
    ) -> bool:
        """领取租约。返回 False 说明这个 run 正被别的执行者持有。

        `statuses` 只在补偿时传 `(FAILED,)` —— 撤回失败计划的写操作,
        要动的正是一个已经失败的 run。
        """
        async with self._sessions() as session:
            claimed = await lease.claim_runs(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.lease_seconds,
                run_uid=run_uid,
                statuses=statuses,
                count_attempt=count_attempt,
                mark_running=mark_running,
            )
            await session.commit()
        return bool(claimed)

    async def _assert_holds_lease(self, run_uid: str) -> None:
        """没有租约就不许推进。

        这条检查是「同一个 run 只有一个执行者」的最后一道门 ——
        API 内联执行、多个 worker 副本走的都是同一份代码,谁都不能例外。
        """
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            if run.status.is_terminal:
                return
            owned = run.lease_owner == self.worker_id
            expired = run.lease_expires_at is None or run.lease_expires_at < utcnow()
        if not owned or expired:
            raise LeaseLost(f"worker {self.worker_id} 没有 run {run_uid} 的有效租约,拒绝推进")

    async def _drive(
        self,
        run_uid: str,
        plan: list[PlannedStep],
        result: ExecutionResult,
        heartbeat: _Heartbeat,
    ) -> None:
        while True:
            if heartbeat.lost:
                raise LeaseLost(
                    f"run {run_uid} 的租约已失效,停止推进:{heartbeat.error or '心跳续约失败'}"
                )

            async with self._sessions() as session:
                run = await self._load(session, run_uid)
                status = run.status
                if not status.is_terminal and run.lease_owner != self.worker_id:
                    raise LeaseLost(f"run {run_uid} 的租约已被 {run.lease_owner} 接管,停止推进")
                completed = await self._completed_seqs(run.id)
                # 上游步骤的产出。引用参数靠它解析 —— 从 agent_step.result 里读,
                # 所以崩溃重启、换一个 worker 接着跑,引用照样能取到值。
                step_results = await self._completed_results(run.id)
                # 已收集到的签名,按步骤号。够不够要看策略要求的签名人数,这里只取事实。
                signatures = {int(seq): list(names) for seq, names in (run.approvals or {}).items()}
                step_budget = run.step_budget
                actor = run.actor

            if status.is_terminal:
                result.status = status
                return
            if status is RunStatus.WAITING_APPROVAL:
                result.status = status
                return

            remaining = [step for step in plan if step.seq not in completed]
            if not remaining:
                result.status = await self._finish(run_uid, RunStatus.SUCCEEDED)
                return

            # 预算按「已完成的步骤数」算。超预算不是失败,是**拒绝继续**:
            # 剩下的事需要人来判断,而不是让 agent 无限试下去。
            if len(completed) >= step_budget:
                message = f"run {run_uid} 超出步骤预算 {step_budget},停止推进"
                await self._finish(run_uid, RunStatus.FAILED, error=message)
                await self._after_failure(run_uid, actor=actor, error=message)
                raise RunBudgetExceeded(message, run_uid=run_uid, step_budget=step_budget)

            step = remaining[0]

            # 依赖与引用一起在这里落地:依赖没成功就不许往下走,
            # 引用的值从上游产出里取。两者都必须发生在**裁决之前** ——
            # 金额、风险等级、幂等键全都读参数,拿占位符去裁决等于在评一个假的操作。
            try:
                _assert_deps_done(step, completed)
            except PlanError as exc:
                await self._record_failure(run_uid, step, exc.message, retryable=False)
                result.status = await self._after_failure(run_uid, actor=actor, error=exc.message)
                return

            try:
                step = step.with_args(plan_module.resolve(step.args, step_results))
            except PlanError as exc:
                message = f"第 {step.seq} 步的参数引用解析失败:{exc.message}"
                await self._record_failure(run_uid, step, message, retryable=False)
                result.status = await self._after_failure(run_uid, actor=actor, error=message)
                return

            spec = self.registry.get(step.tool)
            resolved_amount = await self._resolve_amount(spec, step, actor)
            verdict = self._judge(step, actor=actor, resolved_amount=resolved_amount)
            requirement = policy.approval_requirement(
                spec, step.args, settings=self.settings, resolved_amount=resolved_amount
            )
            collected = signatures.get(step.seq, [])

            # 拒绝:一步都不碰业务表。但审计要留痕(见 _record_failure)——
            # 「这次尝试被挡下来了」本身就是要存档的事实。
            if verdict.decision is PolicyDecision.DENY:
                message = f"策略引擎拒绝执行:{verdict.reason}"
                await self._record_failure(run_uid, step, message, retryable=False, verdict=verdict)
                result.status = await self._after_failure(run_uid, actor=actor, error=message)
                return

            # 审批门是「更严的那一个说了算」:计划里标了要审批的照旧要审批;
            # 计划没标的,只要策略裁决要求审批,一样要审批。
            # 换句话说,计划可以让审批更严,但没法让策略放行得更松。
            needs_approval = step.requires_approval or verdict.decision is (
                PolicyDecision.REQUIRE_APPROVAL
            )
            # 签字够数才算批准。大额操作要求两个不同角色,只签了一个仍然挂起 ——
            # 「有人签过」和「签够了」是两件事。
            if needs_approval and len(collected) < requirement.required:
                await self._wait_for_approval(
                    run_uid, step, verdict, requirement.required, collected
                )
                result.status = RunStatus.WAITING_APPROVAL
                return

            outcome = await self._execute_step(run_uid, step, verdict)
            result.outcomes.append(outcome)
            if outcome.status is StepStatus.FAILED:
                # 失败出口统一收尾:先留痕,再按 compensation_mode 决定要不要
                # 把这次执行已经成功的写操作逆序撤回来。
                result.status = await self._after_failure(
                    run_uid, actor=actor, error=outcome.error or "步骤执行失败"
                )
                return

    # ---------- 补偿(Saga) ----------

    async def compensate(
        self, run_uid: str, *, actor: str, force: bool = False
    ) -> CompensationResult:
        """把这条 run 已经成功的写操作按逆序撤回来。

        `force=True` 表示**有人点了头**:补偿动作本身要过策略引擎,
        默认信任等级下它可能被判成「需要人工审批」;人既然已经点了按钮,
        这次就按记名执行(审计记的是这个人的身份,不是"系统").
        """
        # `mark_running=False`:补偿期间状态留在 FAILED。FAILED 不在可领取集合里,
        # 所以就算这个进程在补偿中途被 kill,普通 worker 也领不走它
        # (领走了会跳过已被撤掉的正向步骤、去重跑失败那一步)。
        if not await self.claim(
            run_uid,
            statuses=(RunStatus.FAILED,),
            count_attempt=False,
            mark_running=False,
        ):
            raise RuleViolation(f"run {run_uid} 现在不能被补偿(只允许对已失败的执行做补偿)")
        return await self._compensate_locked(run_uid, actor=actor, force=force)

    async def _after_failure(self, run_uid: str, *, actor: str, error: str) -> RunStatus:
        """失败出口的统一收尾:按 `compensation_mode` 决定要不要自动撤销。

        - `off`    : 只留失败现场。
        - `manual` : 留失败现场,等人工走 `POST /api/runs/{uid}/compensate`。
        - `auto`   : 立刻按声明逆序补偿。撤不干净会被 `compensation.plan` 的
                     blockers 挡下来 —— 宁可不撤,也不做部分补偿。

        补偿同样要**自己领租约**(`mark_running=False`):`_record_failure` 已经把
        终态 run 的租约交还了,不领的话自动补偿期间会出现"租约空着"的窗口,
        另一个 `POST /compensate` 可以合法插进来。auto 和 manual 走同一条规则:
        补偿必须持租约,但不必把状态改成 RUNNING。
        """
        if self.settings.compensation_mode != "auto":
            return RunStatus.FAILED
        if not await self.claim(
            run_uid,
            statuses=(RunStatus.FAILED,),
            count_attempt=False,
            mark_running=False,
        ):
            # 有人正在补偿这条 run:把终态留给它,别抢。
            return RunStatus.FAILED
        outcome = await self._compensate_locked(
            run_uid, actor=actor, force=False, origin_error=error
        )
        return outcome.status

    async def _compensate_locked(
        self, run_uid: str, *, actor: str, force: bool, origin_error: str | None = None
    ) -> CompensationResult:
        """按声明的补偿动作逆序回滚。撤不干净就停下来告诉人,不做部分补偿。

        `origin_error` 是这条 run 当初为什么失败。补偿成功时也把它留在 `last_error`
        上 —— 否则一次"成功回滚"会把「为什么需要回滚」的理由抹掉。
        """
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            completed = await self._completed_write_steps(session, run.id)
            trace_id = run.trace_id
            origin_error = origin_error or run.last_error

        plan = compensation.plan(
            [
                compensation.CompletedStep(seq=item.seq, tool=item.tool_name, result=item.result)
                for item in completed
            ],
            registry=self.registry,
        )

        if plan.blockers:
            message = _join_reasons(origin_error, "无法自动补偿:" + ";".join(plan.blockers))
            await self._finish(
                run_uid,
                RunStatus.FAILED,
                error=message,
                compensation=self._compensation_state("BLOCKED", plan.steps),
            )
            return CompensationResult(status=RunStatus.FAILED, blockers=plan.blockers)

        if not plan.steps:
            # 没有任何写操作需要撤:状态保持 FAILED(改成 COMPENSATED 会谎称"撤回过")。
            # 但必须落一次终态把租约交还 —— 否则这条 run 一直占着租约,
            # 过期后还会被当普通 run 重新领取执行。
            await self._finish(
                run_uid,
                RunStatus.FAILED,
                error=origin_error,
                compensation=self._compensation_state("NOTHING_TO_ROLLBACK", []),
            )
            return CompensationResult(status=RunStatus.FAILED, blockers=[])

        outcomes: list[StepOutcome] = []
        for step in plan.steps:
            verdict = self._judge(
                PlannedStep(seq=step.seq, tool=step.tool, args=step.args, kind=StepKind.COMPENSATE),
                actor=actor,
            )
            if verdict.decision is PolicyDecision.DENY:
                message = _join_reasons(origin_error, f"补偿被策略拒绝:{verdict.reason}")
                await self._finish(
                    run_uid,
                    RunStatus.FAILED,
                    error=message,
                    compensation=self._compensation_state("BLOCKED", plan.steps),
                )
                return CompensationResult(
                    status=RunStatus.FAILED, outcomes=outcomes, blockers=[message]
                )
            if verdict.decision is PolicyDecision.REQUIRE_APPROVAL and not force:
                # 自动补偿到此为止:补偿也是写操作,权限不够就得有人签字。
                # 注意这里**不执行任何一步**:部分补偿比不补偿更难排查。
                message = (
                    f"已失败的步骤可以按声明撤销,但补偿动作 {step.tool} 在当前权限下需要人工确认:"
                    f"请由有权限的人调用 POST /api/runs/{run_uid}/compensate"
                )
                await self._finish(
                    run_uid,
                    RunStatus.FAILED,
                    error=_join_reasons(origin_error, message),
                    compensation=self._compensation_state("NEEDS_APPROVAL", plan.steps),
                )
                return CompensationResult(
                    status=RunStatus.FAILED, outcomes=outcomes, blockers=[message]
                )

            outcome = await self._execute_compensation_step(
                run_uid, step, actor=actor, trace_id=trace_id, verdict=verdict
            )
            outcomes.append(outcome)
            self._chaos(CHAOS_AFTER_COMPENSATION_COMMIT, step.source_seq)
            if outcome.status is StepStatus.FAILED:
                message = _join_reasons(
                    origin_error,
                    f"补偿第 {step.source_seq} 步失败:{outcome.error};"
                    "已经撤掉的部分不会自动回滚回去 —— 需要人工核对",
                )
                # PARTIAL 是唯一"下次不许重试"的补偿状态:库里有撤了一半的东西,
                # 再重放正向步骤,系统状态就没人验证过了。
                await self._finish(
                    run_uid,
                    RunStatus.FAILED,
                    error=message,
                    compensation=self._compensation_state("PARTIAL", plan.steps),
                )
                return CompensationResult(
                    status=RunStatus.FAILED, outcomes=outcomes, blockers=[message]
                )

        return CompensationResult(
            status=await self._finish(
                run_uid,
                RunStatus.COMPENSATED,
                error=origin_error,
                compensation=self._compensation_state("COMPENSATED", plan.steps),
            ),
            outcomes=outcomes,
        )

    @staticmethod
    def _compensation_state(
        status: str, steps: Sequence[compensation.CompensationStep]
    ) -> dict[str, Any]:
        """「补偿走到哪一步了」的落库形态。

        **必须落库**:补偿可能是异步的、可能要人签字、可能撤到一半失败。
        只写日志的话,下一个打开这条 run 的人(或下一个 worker)无从知道
        库里现在到底是全撤了、没撤、还是撤了一半。
        """
        return {
            "status": status,
            "planned": [
                {"seq": item.seq, "tool": item.tool, "reason": item.reason} for item in steps
            ],
            "at": utcnow().isoformat(),
        }

    async def _execute_compensation_step(
        self,
        run_uid: str,
        step: compensation.CompensationStep,
        *,
        actor: str,
        trace_id: str,
        verdict: PolicyVerdict,
    ) -> StepOutcome:
        """执行一条补偿指令。和正向步骤共用幂等账本,所以重复触发不会撤两次。"""
        spec = self.registry.get(step.tool)
        planned = PlannedStep(
            seq=step.seq, tool=step.tool, args=step.args, kind=StepKind.COMPENSATE
        )
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            key = idempotency.compute_key(
                run_uid=run.run_uid, tool_name=spec.name, arguments=step.args
            )
            try:
                step_row = await self._open_step(session, run_id=run.id, step=planned)
                context = ToolContext(session=session, actor=actor, run_id=run.run_uid)
                claim = await idempotency.claim(
                    session,
                    key=key,
                    tool_name=spec.name,
                    run_uid=run.run_uid,
                    arguments=step.args,
                    owner=self.worker_id,
                )
                if claim.replayed:
                    outcome = await self._commit_replay(
                        session, run, step_row, planned, claim.record.result, key
                    )
                    return outcome

                params = self.registry.validate_args(spec.name, step.args)
                before = await spec.capture(context, params)
                result = await self.registry.invoke(spec.name, context, step.args, params=params)
                after = await spec.capture(context, params)
                payload = result.model_dump(mode="json")

                await audit.append(
                    session,
                    trace_id=trace_id,
                    run_uid=run.run_uid,
                    step_seq=step.seq,
                    actor=actor,
                    tool_name=spec.name,
                    risk_level=spec.risk_level.value,
                    arguments=step.args,
                    before=before,
                    after=after,
                    reason=step.reason,
                    outcome=AuditOutcome.SUCCEEDED,
                    policy_decision="COMPENSATE",
                    policy_reason=step.reason,
                )
                await self._close_step(
                    session, step_row, result=payload, idempotency_key=key, replayed=False
                )
                await idempotency.succeed(session, record=claim.record, result=payload)
                await session.commit()
                return StepOutcome(
                    seq=step.seq,
                    tool=spec.name,
                    status=StepStatus.SUCCEEDED,
                    result=payload,
                    idempotency_key=key,
                )
            except Exception as exc:
                await session.rollback()
                # 补偿失败也要留痕,但它的审计归属和正向失败不一样:
                # 动手的是补偿发起人(可能是人),裁决理由也要写明这是补偿。
                return await self._record_failure(
                    run_uid,
                    planned,
                    f"补偿失败:{exc}",
                    retryable=False,
                    verdict=verdict,
                    actor=actor,
                    policy_decision="COMPENSATE",
                )

    @staticmethod
    async def _completed_write_steps(session: AsyncSession, run_id: int) -> list[AgentStep]:
        """已成功、且**有副作用**的步骤(要回滚的就是它们)。"""
        rows = await session.execute(
            select(AgentStep)
            .where(AgentStep.run_id == run_id, AgentStep.status == StepStatus.SUCCEEDED)
            .order_by(AgentStep.seq)
        )
        return [row for row in rows.scalars().all() if row.kind is StepKind.EXECUTE]

    # ---------- 单步执行 ----------

    async def retry_step(self, run_uid: str, seq: int) -> StepOutcome:
        """重跑指定的某一步。**这是幂等账本真正被用上的地方。**

        正常续跑靠 checkpoint 跳过已成功的步骤,所以不会重复执行;
        但「人工点重试」「上游超时后重发」「并发提交同一意图」这些情况下,
        步骤会被真的再跑一次 —— 此时唯一拦住重复副作用的就是幂等主键。

        注意这里**刻意不抢租约**:重试是人的操作,不该等 worker 让位;
        并发重试之所以安全,靠的是幂等账本而不是互斥锁。
        """
        step = await self._planned_step(run_uid, seq)

        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            compensated = _retry_blocking_state(run)
        if compensated is not None:
            raise RuleViolation(
                f"run {run_uid} 的补偿已经动过库({compensated}),不能重跑第 {seq} 步:"
                "先撤后重放会把撤掉的写操作又做出来,而系统状态没人验证过。请新建一次执行。"
            )

        # 重试也要看依赖:第 1 步没成功就重试第 2 步,重试出来的结果没有意义 ——
        # 「人工点一下」不是绕过编排规则的通行证。
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            completed = await self._completed_seqs_in(session, run.id)
            step_results = await self._completed_results_in(session, run.id)
        _assert_deps_done(step, completed)
        step = step.with_args(plan_module.resolve(step.args, step_results))

        spec = self.registry.get(step.tool)

        # 人工重试本身视为对该步的确认,所以不再要求走一次审批;
        # 但 DENY 是策略层的禁止,人工重试也不能把它点开。
        verdict = self._judge(step, actor=await self.actor_of(run_uid))
        if verdict.decision is PolicyDecision.DENY:
            return StepOutcome(
                seq=step.seq,
                tool=step.tool,
                status=StepStatus.FAILED,
                error=f"策略引擎拒绝执行:{verdict.reason}",
            )

        if not spec.is_read_only:
            key = idempotency.compute_key(run_uid=run_uid, tool_name=step.tool, arguments=step.args)
            async with self._sessions() as session:
                record = await idempotency.lookup(session, key)
            if record is not None and record.status is IdempotencyStatus.SUCCEEDED:
                logger.info("run=%s step=%d 命中幂等账本,复用历史结果", run_uid, seq)

        return await self._execute_step(run_uid, step, verdict)

    def _judge(
        self, step: PlannedStep, *, actor: str, resolved_amount: int | None = None
    ) -> PolicyVerdict:
        """按「工具声明 + 执行体信任等级」现算一次裁决。

        刻意放在执行时而不是计划时:计划可能是几分钟前写的,而权限是现在生效的。
        计划里冻结一个 ALLOW,等于给越权留了一个时间窗。
        """
        return policy.evaluate(
            self.registry.get(step.tool),
            step.args,
            actor=actor,
            settings=self.settings,
            resolved_amount=resolved_amount,
        )

    async def _resolve_amount(self, spec: ToolSpec, step: PlannedStep, actor: str) -> int | None:
        """参数里没写金额时,问工具自己这笔操作实际动多少钱(可能要查库)。

        这一步必须发生在审批门之前 —— 不然一笔小额退款会因为"金额未知"
        被误判成需要双人复核,而一笔没传金额的大额退款可能正好相反。
        """
        if spec.amount_field is None or step.args.get(spec.amount_field) is not None:
            return None
        async with self._sessions() as session:
            return await resolve_effective_amount(spec, step.args, actor=actor, session=session)

    async def actor_of(self, run_uid: str) -> str:
        """这条 run 的执行体身份。补偿要记名时也用它 —— 审计里写的是补偿发起人。"""
        async with self._sessions() as session:
            return (await self._load(session, run_uid)).actor

    async def _planned_step(self, run_uid: str, seq: int) -> PlannedStep:
        from guardrail_api.domain.errors import NotFound

        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            planned = {int(raw["seq"]): raw for raw in run.plan}
        if seq not in planned:
            raise NotFound(f"run {run_uid} 的计划里没有第 {seq} 步")
        return PlannedStep.from_dict(planned[seq])

    async def _execute_step(
        self, run_uid: str, step: PlannedStep, verdict: PolicyVerdict
    ) -> StepOutcome:
        self._chaos(CHAOS_BEFORE_STEP_TXN, step.seq)

        spec = self.registry.get(step.tool)
        failure: Exception | None = None
        outcome: StepOutcome | None = None
        key: str | None = None

        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            if not spec.is_read_only:
                key = idempotency.compute_key(
                    run_uid=run.run_uid, tool_name=step.tool, arguments=step.args
                )
            try:
                step_row = await self._open_step(session, run_id=run.id, step=step)
                context = ToolContext(session=session, actor=run.actor, run_id=run.run_uid)
                # 这一步是谁批的。不记这一笔的话,审计里只留下一句"必须人工审批",
                # 读起来像是被拦住了;而且复盘时答不出「谁为这次写操作签的字」。
                approvers = run.approvers_of(step.seq)

                claim = None
                if key is not None:
                    claim = await idempotency.claim(
                        session,
                        key=key,
                        tool_name=step.tool,
                        run_uid=run.run_uid,
                        arguments=step.args,
                        owner=self.worker_id,
                    )
                    if claim.replayed:
                        # 这一步之前已经成功过:复用历史结果,不碰业务表,不写审计
                        outcome = await self._commit_replay(
                            session, run, step_row, step, claim.record.result, key
                        )
                    else:
                        if claim.record.status is not IdempotencyStatus.IN_FLIGHT:
                            await idempotency.reopen(
                                session, record=claim.record, owner=self.worker_id
                            )

                if outcome is None:
                    payload, before, after, reason = await self._invoke(spec, context, step, run)
                    if claim is not None:
                        await idempotency.succeed(session, record=claim.record, result=payload)
                    if before is not None:
                        await audit.append(
                            session,
                            trace_id=run.trace_id,
                            run_uid=run.run_uid,
                            step_seq=step.seq,
                            actor=run.actor,
                            tool_name=step.tool,
                            risk_level=spec.risk_level.value,
                            arguments=step.args,
                            before=before,
                            after=after,
                            reason=reason,
                            outcome=AuditOutcome.SUCCEEDED,
                            policy_decision=verdict.decision.value,
                            policy_reason=_policy_reason(verdict, approvers),
                        )
                    await self._close_step(
                        session, step_row, result=payload, idempotency_key=key, replayed=False
                    )
                    await self._checkpoint(session, run, step, payload)
                    outcome = StepOutcome(
                        seq=step.seq,
                        tool=step.tool,
                        status=StepStatus.SUCCEEDED,
                        result=payload,
                        idempotency_key=key,
                    )

                await session.commit()
            except Exception as exc:
                await session.rollback()
                failure = exc

        if failure is None:
            assert outcome is not None
            return outcome

        message = getattr(failure, "message", None) or f"{type(failure).__name__}: {failure}"
        await self._record_failure(
            run_uid,
            step,
            message,
            retryable=not isinstance(failure, DomainError),
            verdict=verdict,
        )
        if isinstance(failure, DomainError):
            return StepOutcome(
                seq=step.seq,
                tool=step.tool,
                status=StepStatus.FAILED,
                error=message,
                cause=failure,
            )
        raise failure

    async def _invoke(
        self,
        spec: Any,
        context: ToolContext,
        step: PlannedStep,
        run: AgentRun,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """调用工具并采集审计 diff。前后各取一次快照,中间什么都不做。"""
        params = self.registry.validate_args(step.tool, step.args)
        before = await spec.capture(context, params)
        result = await self.registry.invoke(step.tool, context, step.args, params=params)
        after = await spec.capture(context, params)
        self._chaos(CHAOS_AFTER_TOOL_WRITE, step.seq)
        return (
            result.model_dump(mode="json"),
            before,
            after,
            spec.extract_reason(params, step.args),
        )

    async def _commit_replay(
        self,
        session: AsyncSession,
        run: AgentRun,
        step_row: AgentStep,
        step: PlannedStep,
        payload: dict[str, Any] | None,
        key: str,
    ) -> StepOutcome:
        await self._close_step(
            session, step_row, result=payload, idempotency_key=key, replayed=True
        )
        await self._checkpoint(session, run, step, payload)
        return StepOutcome(
            seq=step.seq,
            tool=step.tool,
            status=StepStatus.SUCCEEDED,
            replayed=True,
            result=payload,
            idempotency_key=key,
        )

    # ---------- 落库细节 ----------

    async def _open_step(
        self, session: AsyncSession, *, run_id: int, step: PlannedStep
    ) -> AgentStep:
        """把步骤置为 RUNNING。attempt 记的是**提交过**的尝试次数,回滚的尝试不留痕。

        用 UPSERT 而不是「先查再插」:两个并发调用同一步骤时,
        「查到没有 → 都去插」必然撞唯一约束。原子写在数据库侧收敛,
        第二个调用会阻塞到第一个提交,然后走 attempt+1 的更新分支。
        """
        now = utcnow()
        statement = (
            pg_insert(AgentStep)
            .values(
                run_id=run_id,
                seq=step.seq,
                kind=step.kind,
                status=StepStatus.RUNNING,
                tool_name=step.tool,
                args=step.args,
                attempt=1,
                started_at=now,
            )
            .on_conflict_do_update(
                index_elements=[AgentStep.run_id, AgentStep.seq],
                set_={
                    "status": StepStatus.RUNNING,
                    "attempt": AgentStep.attempt + 1,
                    "started_at": now,
                    "error": None,
                    "replayed": False,
                },
            )
            .returning(AgentStep.id)
        )
        row_id = (await session.execute(statement)).scalar_one()
        return (
            await session.execute(
                select(AgentStep)
                .where(AgentStep.id == row_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

    async def _close_step(
        self,
        session: AsyncSession,
        row: AgentStep,
        *,
        result: dict[str, Any] | None,
        idempotency_key: str | None,
        replayed: bool,
    ) -> None:
        row.status = StepStatus.SUCCEEDED
        row.result = result
        row.error = None
        row.replayed = replayed
        row.idempotency_key = idempotency_key
        row.finished_at = utcnow()
        await session.flush()

    async def _checkpoint(
        self,
        session: AsyncSession,
        run: AgentRun,
        step: PlannedStep,
        payload: dict[str, Any] | None,
    ) -> None:
        """落 checkpoint。这一步是「被读」的:重启后由它决定从哪继续。

        两个不能退让的细节:
        - checkpoint 只前进不回退。重放一个**更早**的步骤(人工重试)不该把进度标记拽回去,
          否则恢复时会从已经做完的地方重新开始。
        - 已经终态的 run 不因为重放被改回 RUNNING。重试是补一次动作,不是把执行重新打开。
        """
        if run.status.is_terminal:
            # 终态 run 的断点/进度是历史记录,不能被一次人工重试或补偿重放改写。
            # (状态翻转本来就有下面的守卫,这里连 checkpoint 一起挡住 ——
            #  否则审计里会出现"已经成功的 run,第 1 步又被记了一遍"。)
            return

        completed = await self._completed_seqs_in(session, run.id)
        completed.add(step.seq)
        run.checkpoint_seq = max(run.checkpoint_seq, step.seq)
        # 合并而不是整份重写:checkpoint 里除了"最后一步",还挂着补偿这类运行态标记,
        # 覆盖式写入会在下一步成功时把它悄悄抹掉(见 _mark_compensation)。
        run.checkpoint = {
            **(run.checkpoint or {}),
            "seq": run.checkpoint_seq,
            "tool": step.tool,
            "completed": sorted(completed),
            "result_digest": _digest(payload),
            "at": utcnow().isoformat(),
        }
        if not run.status.is_terminal:
            run.status = RunStatus.RUNNING
            run.last_error = None
        run.lease_expires_at = utcnow() + timedelta(seconds=self.settings.lease_seconds)
        await session.flush()

    async def _record_failure(
        self,
        run_uid: str,
        step: PlannedStep,
        message: str,
        *,
        retryable: bool,
        verdict: PolicyVerdict | None = None,
        actor: str | None = None,
        policy_decision: str | None = None,
    ) -> StepOutcome:
        """失败留痕,并把这次的失败原样返回给调用方。

        失败也写审计 —— 只记成功等于把事故现场擦干净。
        `actor` / `policy_decision` 两个口子是给补偿用的:补偿失败时动手的是
        补偿发起人,裁决理由也要写明这是补偿(见 `_execute_compensation_step`)。
        """
        spec = self.registry.get(step.tool)
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            row = await self._open_step(session, run_id=run.id, step=step)
            row.status = StepStatus.FAILED
            row.error = message
            row.finished_at = utcnow()
            await session.flush()

            if not spec.is_read_only:
                await audit.append(
                    session,
                    trace_id=run.trace_id,
                    run_uid=run.run_uid,
                    step_seq=step.seq,
                    actor=actor or run.actor,
                    tool_name=step.tool,
                    risk_level=spec.risk_level.value,
                    arguments=step.args,
                    before=None,
                    after=None,
                    reason=message,
                    outcome=AuditOutcome.FAILED,
                    policy_decision=policy_decision
                    or (verdict.decision.value if verdict else None),
                    policy_reason=verdict.reason if verdict else None,
                )

            exhausted = run.attempt >= self.settings.max_step_retries
            if retryable and not exhausted:
                # 交给下一个 poll 重试:指数退避,避免坏任务把 worker 打满
                backoff = min(2 ** max(run.attempt, 1), 60)
                await lease.defer(
                    session, run_uid=run_uid, worker_id=self.worker_id, seconds=backoff
                )
            else:
                if not run.status.is_terminal:
                    # 终态 run 不因为一次人工重试失败就被推翻:重试是补一次动作,
                    # 不是把一条已经成功的执行改写成失败(那次失败会在 agent_step
                    # 和审计里留下,但不改 run 的结论)。
                    run.status = RunStatus.FAILED
                    run.last_error = message
                    run.finished_at = utcnow()
                # 终态不留租约。留着它,刚失败的 run 就补偿不了 ——
                # `compensate()` 要求租约空闲或过期,而它正是"失败之后"才被调用的。
                await lease.release(session, run_uid=run_uid, worker_id=self.worker_id)
            await session.commit()
        return StepOutcome(
            seq=step.seq,
            tool=step.tool,
            status=StepStatus.FAILED,
            error=message,
        )

    async def _wait_for_approval(
        self,
        run_uid: str,
        step: PlannedStep,
        verdict: PolicyVerdict,
        required: int = 1,
        collected: list[str] | None = None,
    ) -> None:
        """挂起等人工。**把裁决理由和已收集的签名一起存下来** ——
        审批人要知道自己在批什么、还差几个人。"""
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            run.status = RunStatus.WAITING_APPROVAL
            run.waiting_ref = f"step:{step.seq}:{step.tool}"
            run.checkpoint = {
                "seq": run.checkpoint_seq,
                "waiting_for": step.to_dict(),
                "policy": verdict.to_dict(),
                "approval": {
                    "required": required,
                    "collected": list(collected or []),
                    "remaining": max(required - len(collected or []), 0),
                },
                "at": utcnow().isoformat(),
            }
            # **等审批不等于在执行,不能占着租约。** 占着的话审批人点完「批准」之后
            # 没有任何 worker 能领取它,得干等租约过期 —— 一个纯人造的 30 秒延迟。
            await lease.release(session, run_uid=run_uid, worker_id=self.worker_id)
            await session.commit()

    async def _finish(
        self,
        run_uid: str,
        status: RunStatus,
        *,
        error: str | None = None,
        compensation: dict[str, Any] | None = None,
    ) -> RunStatus:
        """落终态 + 交还租约。`compensation` 会和状态**同一次提交**写进 checkpoint。

        分两次提交会在中间留一个可被 kill 的窗口:标记说"撤过了",状态还是旧的。
        幂等账本兜不住这种分裂 —— 它管写入,不管状态标记。
        """
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            run.status = status
            run.last_error = error
            run.finished_at = utcnow()
            if compensation is not None:
                run.checkpoint = {**(run.checkpoint or {}), "compensation": compensation}
            await session.flush()
            await lease.release(session, run_uid=run_uid, worker_id=self.worker_id)
            await session.commit()
        return status

    # ---------- 只读查询 ----------

    async def _load(self, session: AsyncSession, run_uid: str) -> AgentRun:
        from guardrail_api.domain.errors import NotFound

        run = (
            await session.execute(select(AgentRun).where(AgentRun.run_uid == run_uid))
        ).scalar_one_or_none()
        if run is None:
            raise NotFound(f"执行记录 {run_uid} 不存在")
        return run

    async def _completed_seqs(self, run_id: int) -> set[int]:
        """**这里是 checkpoint 真正被读的地方。**"""
        async with self._sessions() as session:
            return await self._completed_seqs_in(session, run_id)

    @staticmethod
    async def _completed_seqs_in(session: AsyncSession, run_id: int) -> set[int]:
        """已成功的步骤序号。同一个会话里调用时能看到刚 flush 的行。"""
        rows = await session.execute(
            select(AgentStep.seq).where(
                AgentStep.run_id == run_id,
                AgentStep.status == StepStatus.SUCCEEDED,
                # 补偿步骤用负数 seq、不在计划里:混进来会写进 checkpoint["completed"],
                # 还会把 len(completed) >= step_budget 的预算判断虚增。
                AgentStep.kind != StepKind.COMPENSATE,
            )
        )
        return set(rows.scalars().all())

    async def _completed_results(self, run_id: int) -> dict[int, dict[str, Any]]:
        """已成功步骤的产出,按步骤号。参数引用从这里取值。"""
        async with self._sessions() as session:
            return await self._completed_results_in(session, run_id)

    @staticmethod
    async def _completed_results_in(
        session: AsyncSession, run_id: int
    ) -> dict[int, dict[str, Any]]:
        return await load_step_results(session, run_id)

    async def _heartbeat_loop(self, run_uid: str, heartbeat: _Heartbeat) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                async with self._sessions() as session:
                    alive = await lease.heartbeat(
                        session,
                        run_uid=run_uid,
                        worker_id=self.worker_id,
                        lease_seconds=self.settings.lease_seconds,
                    )
                    await session.commit()
            except Exception as exc:
                heartbeat.lost = True
                heartbeat.error = f"{type(exc).__name__}: {exc}"
                return
            if not alive:
                heartbeat.lost = True
                heartbeat.error = "租约已被其他 worker 接管"
                return

    def _chaos(self, event: str, seq: int) -> None:
        if self.chaos is not None:
            self.chaos(event, seq)


async def load_step_results(session: AsyncSession, run_id: int) -> dict[int, dict[str, Any]]:
    """已成功步骤的产出,按步骤号。参数里的 `$ref` 从这里取值。

    放在模块级而不是 executor 的方法里,是因为**审批路由**也要用它:
    审批门要算「金额多大、要几个人签字」,而金额可能是个引用占位符 ——
    拿占位符去算,一笔小额定金会被当成"金额未知",于是永远签不够。
    """
    rows = await session.execute(
        select(AgentStep.seq, AgentStep.result).where(
            AgentStep.run_id == run_id, AgentStep.status == StepStatus.SUCCEEDED
        )
    )
    return {int(seq): dict(result or {}) for seq, result in rows.all()}


def _assert_deps_done(step: PlannedStep, completed: set[int]) -> None:
    """前置步骤没成功就不许执行这一步。

    依赖只能指向更早的步骤,而执行顺序就是 seq 顺序,所以走到这一步时,
    依赖要么已经成功,要么永远不会成功 —— 不存在"再等等"的中间态。
    失败要说得具体:**哪一步、依赖谁**,而不是一句"前置条件不满足"。
    """
    missing = sorted(dep for dep in step.depends_on if dep not in completed)
    if missing:
        raise PlanError(
            f"第 {step.seq} 步依赖第 {missing} 步,但那些步骤没有成功;"
            "拒绝执行 —— 前置没做完就往下走,后面每一步都是错的",
            seq=step.seq,
            missing=missing,
            dependency=True,
        )


def _policy_reason(verdict: PolicyVerdict, approvers: list[str]) -> str:
    """审计里的裁决理由。有人批过就补一句 —— 否则读起来像"被拦住了",而它其实执行了。

    签名人数一起写进去:双人复核的意义就是"不止一个人点了头",审计得看得出来。
    """
    if approvers:
        who = "、".join(approvers)
        return f"{verdict.reason};该操作已获 {who} 批准后执行(共 {len(approvers)} 人签字)"
    return verdict.reason


def _join_reasons(*parts: str | None) -> str:
    """把「为什么失败」和「为什么撤不掉」拼起来 —— 合成一条也不该丢掉另一半。"""
    return ";".join(item for item in parts if item)


#: 补偿走到这两个状态之后,这条 run 就不再接受「重跑某一步」。
#: 「撤掉过东西」和「重放某一步」是互斥的:先撤后重放,库里会多出
#: 一个系统以为已经被撤掉的副作用。NEEDS_APPROVAL / BLOCKED /
#: NOTHING_TO_ROLLBACK 都还没有动过库,重试是安全的,只做提示不做拦截。
RETRY_BLOCKING_COMPENSATION_STATES = frozenset({"COMPENSATED", "PARTIAL"})


def _retry_blocking_state(run: AgentRun) -> str | None:
    """这条 run 现在还能不能重跑某一步?返回 None 表示可以。"""
    state = (run.checkpoint or {}).get("compensation") or {}
    status = str(state.get("status") or "")
    if run.status is RunStatus.COMPENSATED:
        return str(state.get("status") or RunStatus.COMPENSATED.value)
    if status in RETRY_BLOCKING_COMPENSATION_STATES:
        return status
    return None


def _digest(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
