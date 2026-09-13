"""售后工单 REST 路由:审批中心的数据来源。"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.api.deps import ActorDep
from guardrail_api.db import get_session
from guardrail_api.models import AftersalesTicket, Order, TicketStatus, TicketType
from guardrail_api.services import aftersales

router = APIRouter(prefix="/api/tickets", tags=["aftersales"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class TicketSummary(BaseModel):
    ticket_id: int
    ticket_no: str
    order_id: int
    order_no: str
    status: TicketStatus
    type: TicketType
    reason_code: str
    refund_amount_cents: int | None = None
    requested_at: datetime
    handled_by: str | None = None
    reject_reason: str | None = None


class TicketListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TicketSummary]


class RejectTicketRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


async def _summaries(session: AsyncSession, tickets: list[AftersalesTicket]) -> list[TicketSummary]:
    if not tickets:
        return []

    order_ids = {ticket.order_id for ticket in tickets}
    order_nos = dict(
        (
            await session.execute(select(Order.id, Order.order_no).where(Order.id.in_(order_ids)))
        ).all()
    )
    return [_to_summary(ticket, order_nos.get(ticket.order_id, "?")) for ticket in tickets]


def _to_summary(ticket: AftersalesTicket, order_no: str) -> TicketSummary:
    return TicketSummary(
        ticket_id=ticket.id,
        ticket_no=ticket.ticket_no,
        order_id=ticket.order_id,
        order_no=order_no,
        status=ticket.status,
        type=ticket.type,
        reason_code=ticket.reason_code,
        refund_amount_cents=ticket.refund_amount_cents,
        requested_at=ticket.requested_at,
        handled_by=ticket.handled_by,
        reject_reason=ticket.reject_reason,
    )


@router.get("", response_model=TicketListResponse, summary="售后工单列表")
async def list_tickets(
    session: SessionDep,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketListResponse:
    filters = [AftersalesTicket.status == status] if status is not None else []

    total = int(
        await session.scalar(select(func.count()).select_from(AftersalesTicket).where(*filters))
        or 0
    )
    tickets = list(
        await session.scalars(
            select(AftersalesTicket)
            .where(*filters)
            .order_by(AftersalesTicket.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return TicketListResponse(
        total=total, limit=limit, offset=offset, items=await _summaries(session, tickets)
    )


async def _commit_one(session: AsyncSession, ticket: AftersalesTicket) -> TicketSummary:
    await session.commit()
    summaries = await _summaries(session, [ticket])
    return summaries[0]


@router.post("/{ticket_id}/approve", response_model=TicketSummary, summary="审批通过")
async def approve_ticket(ticket_id: int, session: SessionDep, actor: ActorDep) -> TicketSummary:
    ticket = await aftersales.approve_ticket(session, ticket_id, operator=actor)
    return await _commit_one(session, ticket)


@router.post("/{ticket_id}/reject", response_model=TicketSummary, summary="审批拒绝")
async def reject_ticket(
    ticket_id: int, body: RejectTicketRequest, session: SessionDep, actor: ActorDep
) -> TicketSummary:
    ticket = await aftersales.reject_ticket(session, ticket_id, operator=actor, reason=body.reason)
    return await _commit_one(session, ticket)


@router.post("/{ticket_id}/refund", response_model=TicketSummary, summary="执行退款")
async def execute_refund(ticket_id: int, session: SessionDep, actor: ActorDep) -> TicketSummary:
    ticket = await aftersales.execute_refund(session, ticket_id, operator=actor)
    return await _commit_one(session, ticket)


@router.post("/{ticket_id}/close", response_model=TicketSummary, summary="关闭工单")
async def close_ticket(ticket_id: int, session: SessionDep, actor: ActorDep) -> TicketSummary:
    ticket = await aftersales.close_ticket(session, ticket_id, operator=actor)
    return await _commit_one(session, ticket)
