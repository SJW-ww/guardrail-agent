"""审计查询路由。

审计是**只读**的:这里没有 POST / PUT / DELETE。
配合数据库上的触发器,「改一改审计日志」这件事在两个层面上都做不到。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.db import get_session
from guardrail_api.models import AuditLog, AuditOutcome

router = APIRouter(prefix="/api/audit", tags=["governance"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class AuditEntry(BaseModel):
    id: int
    trace_id: str
    run_uid: str | None = None
    step_seq: int | None = None
    actor: str
    tool_name: str
    risk_level: str
    args: dict[str, Any]
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
    outcome: AuditOutcome
    created_at: datetime


class AuditListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AuditEntry]


@router.get("", response_model=AuditListResponse, summary="写操作审计流水")
async def list_audit(
    session: SessionDep,
    run_uid: str | None = None,
    trace_id: str | None = None,
    tool_name: str | None = None,
    outcome: AuditOutcome | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditListResponse:
    conditions = []
    if run_uid is not None:
        conditions.append(AuditLog.run_uid == run_uid)
    if trace_id is not None:
        conditions.append(AuditLog.trace_id == trace_id)
    if tool_name is not None:
        conditions.append(AuditLog.tool_name == tool_name)
    if outcome is not None:
        conditions.append(AuditLog.outcome == outcome)

    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    rows = (
        (
            await session.execute(
                select(AuditLog)
                .where(*conditions)
                .order_by(AuditLog.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return AuditListResponse(
        total=int(total or 0),
        limit=limit,
        offset=offset,
        items=[AuditEntry.model_validate(row, from_attributes=True) for row in rows],
    )


@router.get("/{entry_id}", response_model=AuditEntry, summary="单条审计(前后值对比)")
async def get_entry(entry_id: int, session: SessionDep) -> AuditEntry:
    from guardrail_api.domain.errors import NotFound

    row = await session.get(AuditLog, entry_id)
    if row is None:
        raise NotFound(f"审计记录 {entry_id} 不存在")
    return AuditEntry.model_validate(row, from_attributes=True)
