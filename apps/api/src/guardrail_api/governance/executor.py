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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from guardrail_api.config import Settings, get_settings
from guardrail_api.domain.clock import utcnow
from guardrail_api.domain.errors import DomainError, LeaseLost, PlanError, RunBudgetExceeded
from guardrail_api.governance import audit, idempotency, lease, policy
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

    async def claim(self, run_uid: str) -> bool:
        """领取租约。返回 False 说明这个 run 正被别的执行者持有。"""
        async with self._sessions() as session:
            claimed = await lease.claim_runs(
                session,
                worker_id=self.worker_id,
                lease_seconds=self.settings.lease_seconds,
                run_uid=run_uid,
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
                raise RunBudgetExceeded(message, run_uid=run_uid, step_budget=step_budget)

            step = remaining[0]

            # 依赖与引用一起在这里落地:依赖没成功就不许往下走,
            # 引用的值从上游产出里取。两者都必须发生在**裁决之前** ——
            # 金额、风险等级、幂等键全都读参数,拿占位符去裁决等于在评一个假的操作。
            try:
                _assert_deps_done(step, completed)
            except PlanError as exc:
                await self._record_failure(run_uid, step, exc.message, retryable=False)
                await self._finish(run_uid, RunStatus.FAILED, error=exc.message)
                result.status = RunStatus.FAILED
                return

            try:
                step = step.with_args(plan_module.resolve(step.args, step_results))
            except PlanError as exc:
                message = f"第 {step.seq} 步的参数引用解析失败:{exc.message}"
                await self._record_failure(run_uid, step, message, retryable=False)
                await self._finish(run_uid, RunStatus.FAILED, error=message)
                result.status = RunStatus.FAILED
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
                await self._finish(run_uid, RunStatus.FAILED, error=message)
                result.status = RunStatus.FAILED
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
                async with self._sessions() as session:
                    result.status = (await self._load(session, run_uid)).status
                return

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
        verdict = self._judge(step, actor=await self._actor_of(run_uid))
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

    async def _actor_of(self, run_uid: str) -> str:
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
        completed = await self._completed_seqs_in(session, run.id)
        completed.add(step.seq)
        run.checkpoint_seq = max(run.checkpoint_seq, step.seq)
        run.checkpoint = {
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
    ) -> None:
        """失败留痕。失败也写审计 —— 只记成功等于把事故现场擦干净。"""
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
                    actor=run.actor,
                    tool_name=step.tool,
                    risk_level=spec.risk_level.value,
                    arguments=step.args,
                    before=None,
                    after=None,
                    reason=message,
                    outcome=AuditOutcome.FAILED,
                    policy_decision=verdict.decision.value if verdict else None,
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
                run.status = RunStatus.FAILED
                run.last_error = message
                run.finished_at = utcnow()
            await session.commit()

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
        self, run_uid: str, status: RunStatus, *, error: str | None = None
    ) -> RunStatus:
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            run.status = status
            run.last_error = error
            run.finished_at = utcnow()
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
                AgentStep.run_id == run_id, AgentStep.status == StepStatus.SUCCEEDED
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


def _digest(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
