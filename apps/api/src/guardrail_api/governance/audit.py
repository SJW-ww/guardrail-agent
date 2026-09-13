"""审计写入。

审计行只做两件事:记下**谁**(actor)、**凭什么**(reason)、**把什么从什么改成了什么**
(before / after)。其余字段是为了能在事故复盘时把时间线串起来(trace_id / run_uid / step_seq)。

写入点是执行器,不是工具自己。工具作者不需要记得写审计 —— 这是能全量覆盖的前提。
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.models import AuditLog, AuditOutcome


async def append(
    session: AsyncSession,
    *,
    trace_id: str,
    run_uid: str | None,
    step_seq: int | None,
    actor: str,
    tool_name: str,
    risk_level: str,
    arguments: Mapping[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    reason: str | None,
    outcome: AuditOutcome,
) -> AuditLog:
    entry = AuditLog(
        trace_id=trace_id,
        run_uid=run_uid,
        step_seq=step_seq,
        actor=actor,
        tool_name=tool_name,
        risk_level=risk_level,
        args=dict(arguments),
        before=before,
        after=after,
        reason=reason,
        outcome=outcome,
    )
    session.add(entry)
    await session.flush()
    return entry
