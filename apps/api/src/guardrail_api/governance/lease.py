"""租约与心跳。

「同一个 run 不会被两个 worker 同时推进」这件事必须由数据库保证,不能靠进程内的锁 ——
进程会死、会被扩容、会有两个副本。这里用 Postgres 的 `FOR UPDATE SKIP LOCKED`
做原子领取:两个 worker 同时来抢,数据库只会放行一个,另一个直接跳过而不是阻塞排队。

租约是有期限的。worker 必须每 `heartbeat_interval_seconds` 续一次,
续约失败(比如自己的租约已被别人抢走)就必须立刻停手 —— 否则它会在
「已经不属于自己」的数据上继续写。
"""

from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.clock import utcnow
from guardrail_api.models import AgentRun, RunStatus

# 可被领取的状态:WAITING_APPROVAL / 终态都不在里面。
# 等审批的 run 必须由「审批通过」这个业务动作显式置回 PENDING 才能被重新领取。
CLAIMABLE_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)


async def claim_runs(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    limit: int = 1,
    run_uid: str | None = None,
    now: datetime | None = None,
) -> list[AgentRun]:
    """原子领取待执行的 run。租约已过期的 RUNNING 会被视为可领取(崩溃恢复入口)。"""
    now = now or utcnow()
    statement = select(AgentRun).where(
        AgentRun.status.in_(CLAIMABLE_STATUSES),
        or_(
            AgentRun.lease_owner.is_(None),
            AgentRun.lease_expires_at.is_(None),
            AgentRun.lease_expires_at < now,
        ),
    )
    if run_uid is not None:
        statement = statement.where(AgentRun.run_uid == run_uid)
    statement = statement.order_by(AgentRun.id).limit(limit).with_for_update(skip_locked=True)
    runs = list((await session.execute(statement)).scalars().all())
    for run in runs:
        run.lease_owner = worker_id
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        run.heartbeat_at = now
        run.attempt += 1
        # 被领取即视为在跑。心跳的 WHERE 条件里有 status=RUNNING ——
        # 如果这里不落 RUNNING,第一个步骤执行期间的心跳会被自己拒掉。
        run.status = RunStatus.RUNNING
    await session.flush()
    return runs


async def heartbeat(
    session: AsyncSession,
    *,
    run_uid: str,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """续约。返回 False 说明租约已经丢了 —— 调用方必须停止推进这个 run。"""
    now = now or utcnow()
    statement = (
        update(AgentRun)
        .where(
            AgentRun.run_uid == run_uid,
            AgentRun.lease_owner == worker_id,
            AgentRun.status == RunStatus.RUNNING,
        )
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds), heartbeat_at=now)
        .returning(AgentRun.run_uid)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def release(session: AsyncSession, *, run_uid: str, worker_id: str) -> None:
    """主动交还租约。必须带 worker_id 条件 —— 不能替别人释放。"""
    await session.execute(
        update(AgentRun)
        .where(AgentRun.run_uid == run_uid, AgentRun.lease_owner == worker_id)
        .values(lease_owner=None, lease_expires_at=None)
    )
    await session.flush()


async def defer(
    session: AsyncSession,
    *,
    run_uid: str,
    worker_id: str,
    seconds: int,
    now: datetime | None = None,
) -> None:
    """退避重试:保留租约归属,但把到期时间往后推。

    直接把租约清空会让坏任务立刻被下一个 poll 抢回去,变成紧凑重试循环;
    这里用「谁犯的错谁继续负责,但先等一会儿」来避免把 worker 打满。
    """
    now = now or utcnow()
    await session.execute(
        update(AgentRun)
        .where(AgentRun.run_uid == run_uid, AgentRun.lease_owner == worker_id)
        .values(lease_expires_at=now + timedelta(seconds=seconds))
    )
    await session.flush()
