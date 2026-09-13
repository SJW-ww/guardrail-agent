"""幂等账本。

**写在数据库里,不写在应用内存里。** 判定「这次调用是不是重放」依赖的是
`idempotency_key` 表的主键冲突,而不是「先查一下有没有,没有我再写」——
后者在并发下必然漏。

调用协议(全部在同一个事务内完成):

    claim()   -> None          : 我是第一个,去执行
              -> 终态记录        : 别人已经执行过,直接复用它,不再产生副作用
    succeed() / fail()         : 落终态,与业务写、审计写一起提交

`claim()` 用 `INSERT ... ON CONFLICT DO NOTHING`,冲突时 Postgres 会**阻塞**
到对方事务结束。所以读到的记录一定是已提交的终态,不存在「读到 IN_FLIGHT 不知道该不该等」
的中间态。
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.models import IdempotencyRecord, IdempotencyStatus


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """一次抢占的结果。

    `replayed=True` 表示这条 key 之前**已经成功执行过**,调用方必须直接复用
    `record.result`,不允许再碰业务表。
    """

    record: IdempotencyRecord
    replayed: bool


def canonical_args(arguments: Mapping[str, Any]) -> str:
    """规范化序列化:字典序 + 紧凑分隔符。

    同义参数必须得到同一个哈希,否则 `{a:1,b:2}` 与 `{b:2,a:1}` 会各写一次库。
    """
    return json.dumps(
        dict(arguments), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def hash_args(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_args(arguments).encode()).hexdigest()


def compute_key(*, run_uid: str, tool_name: str, arguments: Mapping[str, Any]) -> str:
    """key = hash(run_uid, tool, args)。

    作用域刻意限定在一次 run 内:不同 run 是两次独立意图(用户可能确实想分两次部分退款),
    同一个 run 内的重试才是「同一次操作」。跨 run 去重需要业务级幂等键,
    那是策略层的事,不能在这里一刀切。
    """
    material = f"{run_uid}\x1f{tool_name}\x1f{canonical_args(arguments)}"
    return hashlib.sha256(material.encode()).hexdigest()


async def claim(
    session: AsyncSession,
    *,
    key: str,
    tool_name: str,
    run_uid: str,
    arguments: Mapping[str, Any],
    owner: str | None,
) -> ClaimResult:
    """抢占这个 key。返回的记录可能是自己刚插的,也可能是别人留下的终态。"""
    statement = (
        pg_insert(IdempotencyRecord)
        .values(
            key=key,
            tool_name=tool_name,
            run_uid=run_uid,
            args_hash=hash_args(arguments),
            status=IdempotencyStatus.IN_FLIGHT,
            owner=owner,
        )
        .on_conflict_do_nothing(index_elements=[IdempotencyRecord.key])
        .returning(IdempotencyRecord)
    )
    record = (await session.execute(statement)).scalar_one_or_none()
    if record is not None:
        return ClaimResult(record=record, replayed=False)

    # 冲突分支:对方事务已结束,这里读到的必然是可复用的终态
    existing = (
        await session.execute(
            select(IdempotencyRecord).where(IdempotencyRecord.key == key).with_for_update()
        )
    ).scalar_one()
    return ClaimResult(record=existing, replayed=existing.status is IdempotencyStatus.SUCCEEDED)


async def reopen(session: AsyncSession, *, record: IdempotencyRecord, owner: str | None) -> None:
    """接管一条上次失败的记录。

    失败的尝试**没有产生副作用**(整个事务回滚了),所以重试是安全的;
    不把它当成终态,否则一次网络抖动就会让这个步骤永远无法重跑。
    """
    record.status = IdempotencyStatus.IN_FLIGHT
    record.owner = owner
    record.error = None
    await session.flush()


async def succeed(
    session: AsyncSession, *, record: IdempotencyRecord, result: dict[str, Any]
) -> None:
    record.status = IdempotencyStatus.SUCCEEDED
    record.result = result
    record.error = None
    await session.flush()


async def fail(session: AsyncSession, *, record: IdempotencyRecord, error: str) -> None:
    record.status = IdempotencyStatus.FAILED
    record.error = error
    await session.flush()


async def lookup(session: AsyncSession, key: str) -> IdempotencyRecord | None:
    """只读查一下这个 key 有没有成功过。

    不加锁:真正的判定仍然由 `claim()` 的主键冲突负责,
    这里只是让调用方少做一次明知会重复的无用功。
    """
    return (
        await session.execute(select(IdempotencyRecord).where(IdempotencyRecord.key == key))
    ).scalar_one_or_none()
