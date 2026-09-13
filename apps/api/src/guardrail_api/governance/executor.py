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
from guardrail_api.domain.errors import DomainError, LeaseLost, RunBudgetExceeded
from guardrail_api.governance import audit, idempotency, lease
from guardrail_api.models import (
    AgentRun,
    AgentStep,
    AuditOutcome,
    IdempotencyStatus,
    RunStatus,
    StepKind,
    StepStatus,
)
from guardrail_api.tools.base import ToolContext
from guardrail_api.tools.registry import ToolRegistry, load_tools

logger = logging.getLogger("guardrail.executor")

# 混沌钩子:用于演示/测试「进程被杀」。事件名 + step seq。
# 生产环境永远是 None —— 但它换来的是「崩溃恢复」这条路径可以被自动化验证。
ChaosHook = Callable[[str, int], None]

CHAOS_BEFORE_STEP_TXN = "before_step_txn"
CHAOS_AFTER_TOOL_WRITE = "after_tool_write"


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """计划里的一步。刻意只用 JSON 可表达的结构,方便整条计划落库与回放。"""

    seq: int
    tool: str
    args: dict[str, Any]
    requires_approval: bool = False
    kind: StepKind = StepKind.EXECUTE

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlannedStep":
        return cls(
            seq=int(raw["seq"]),
            tool=str(raw["tool"]),
            args=dict(raw.get("args") or {}),
            requires_approval=bool(raw.get("requires_approval", False)),
            kind=StepKind(raw.get("kind", StepKind.EXECUTE.value)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tool": self.tool,
            "args": self.args,
            "requires_approval": self.requires_approval,
            "kind": self.kind.value,
        }


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
    seqs = [step.seq for step in plan]
    if len(set(seqs)) != len(seqs):
        raise ValueError(f"计划里的 seq 必须唯一:{seqs}")

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
                approved = set(run.approved_seqs or [])
                step_budget = run.step_budget

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
            if step.requires_approval and step.seq not in approved:
                await self._wait_for_approval(run_uid, step)
                result.status = RunStatus.WAITING_APPROVAL
                return

            outcome = await self._execute_step(run_uid, step)
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
        spec = self.registry.get(step.tool)

        if not spec.is_read_only:
            key = idempotency.compute_key(run_uid=run_uid, tool_name=step.tool, arguments=step.args)
            async with self._sessions() as session:
                record = await idempotency.lookup(session, key)
            if record is not None and record.status is IdempotencyStatus.SUCCEEDED:
                logger.info("run=%s step=%d 命中幂等账本,复用历史结果", run_uid, seq)

        return await self._execute_step(run_uid, step)

    async def _planned_step(self, run_uid: str, seq: int) -> PlannedStep:
        from guardrail_api.domain.errors import NotFound

        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            planned = {int(raw["seq"]): raw for raw in run.plan}
        if seq not in planned:
            raise NotFound(f"run {run_uid} 的计划里没有第 {seq} 步")
        return PlannedStep.from_dict(planned[seq])

    async def _execute_step(self, run_uid: str, step: PlannedStep) -> StepOutcome:
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
            run_uid, step, message, retryable=not isinstance(failure, DomainError)
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
        self, run_uid: str, step: PlannedStep, message: str, *, retryable: bool
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

    async def _wait_for_approval(self, run_uid: str, step: PlannedStep) -> None:
        async with self._sessions() as session:
            run = await self._load(session, run_uid)
            run.status = RunStatus.WAITING_APPROVAL
            run.waiting_ref = f"step:{step.seq}:{step.tool}"
            run.checkpoint = {
                "seq": run.checkpoint_seq,
                "waiting_for": step.to_dict(),
                "at": utcnow().isoformat(),
            }
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


def _digest(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
