"""治理层数据模型(W2)。

四张表构成「写操作执行层」的持久化内核:

- `agent_run`       一次 Agent 执行:plan / checkpoint / 租约 / 预算
- `agent_step`      执行中的单个步骤,**每个 step 提交一次事务**
- `idempotency_key` 幂等账本,主键 = hash(run, tool, args)
- `audit_log`       追加写审计,由 DB 触发器禁止 UPDATE / DELETE

设计取舍:治理表只存**事实**(做了什么、改了哪些值、谁批准的),
不存模型的自我叙述。模型的解释可以撒谎,审计行不能。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from guardrail_api.db import Base
from guardrail_api.models.enums import (
    AuditOutcome,
    IdempotencyStatus,
    RunStatus,
    StepKind,
    StepStatus,
    enum_column,
)
from guardrail_api.models.mixins import TimestampMixin


class AgentRun(TimestampMixin, Base):
    """一次 Agent 执行。

    `checkpoint_seq` 是恢复的唯一依据:重启后从「最大的已成功 seq」之后继续,
    checkpoint 会被**读**,不是只写给人看。
    """

    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_uid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus, "run_status"),
        nullable=False,
        default=RunStatus.PENDING,
        index=True,
    )

    # 计划与断点。plan 是「要做什么」,checkpoint 是「已经做完什么」。
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    checkpoint_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # 等审批时挂住的原因(工单号 / 策略理由),恢复时要能原样接着走
    waiting_ref: Mapped[str | None] = mapped_column(String(64))
    # 人工批准过的 step seq。批准是一次**显式事件**,不能被推断 ——
    # 推断意味着「模型自己觉得没问题」也能过
    approved_seqs: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    last_error: Mapped[str | None] = mapped_column(Text)

    # 预算:W3 接 LLM 后启用,先在表里占位,避免上线时改表
    step_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    token_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 租约。worker 必须持续续约,续不上就必须停手 —— 否则会出现两个 worker 同时写。
    lease_owner: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list["AgentStep"]] = relationship(
        back_populates="run", order_by="AgentStep.seq", lazy="selectin"
    )

    __table_args__ = (Index("ix_agent_run_claimable", "status", "lease_expires_at"),)


class AgentStep(TimestampMixin, Base):
    """执行中的单个步骤。

    步骤粒度就是事务粒度:`agent_step` 落 SUCCEEDED 与业务写、审计写同一次提交。
    所以「步骤已成功」这件事永远不会是假的 —— 崩溃时它们一起回滚。
    """

    __tablename__ = "agent_step"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[StepKind] = mapped_column(
        enum_column(StepKind, "step_kind"), nullable=False, default=StepKind.EXECUTE
    )
    status: Mapped[StepStatus] = mapped_column(
        enum_column(StepStatus, "step_status"), nullable=False, default=StepStatus.RUNNING
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    # 同一个 (run, seq) 可以被重试,attempt 记录「试了几次」——
    # 续跑测试要断言的是:已成功的步骤 attempt 不会因为重启而增加。
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 命中幂等账本、直接复用历史结果的一次执行。
    # 重放不写审计 —— 审计表里一行代表「世界上真的发生过一次写」,不能虚报
    replayed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="steps")

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_step_run_seq"),)


class IdempotencyRecord(TimestampMixin, Base):
    """幂等账本。

    主键是 hash(run_uid, tool, args) —— 只在这一张表上做唯一性,
    副作用的重放在 DB 层被物理挡住,不依赖应用层的「先查再写」。
    """

    __tablename__ = "idempotency_key"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_uid: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        enum_column(IdempotencyStatus, "idempotency_status"),
        nullable=False,
        default=IdempotencyStatus.IN_FLIGHT,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(64))


class AuditLog(Base):
    """追加写审计。

    刻意**不用** TimestampMixin:它没有 updated_at,因为这一行永远不该被更新。
    数据库触发器会拒绝 UPDATE / DELETE —— 应用层写错代码也改不掉历史。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_uid: Mapped[str | None] = mapped_column(String(36), index=True)
    step_seq: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)

    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)

    # 这次写操作是在什么裁决下发生的。事后复盘要答的是「当时凭什么允许它写」,
    # 只记「它写了什么」答不了这个问题。
    policy_decision: Mapped[str | None] = mapped_column(String(24))
    policy_reason: Mapped[str | None] = mapped_column(Text)

    outcome: Mapped[AuditOutcome] = mapped_column(
        enum_column(AuditOutcome, "audit_outcome"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
