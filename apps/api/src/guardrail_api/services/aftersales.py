"""售后领域服务:申请 → 审批 → 退款。"""

import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.clock import utcnow
from guardrail_api.domain.errors import NotFound, RuleViolation
from guardrail_api.domain.state import TICKET_TRANSITIONS, assert_transition
from guardrail_api.models import (
    REFUND_REASON_CODES,
    AftersalesTicket,
    Order,
    OrderStatus,
    TicketStatus,
    TicketType,
)

# 只有这些订单状态可以申请退款:未支付和已取消的订单没有钱可退
REFUNDABLE_ORDER_STATUSES = frozenset(
    {OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED}
)

# 占用额度但还没真正退出去的状态
PENDING_REFUND_STATUSES = frozenset({TicketStatus.PENDING, TicketStatus.APPROVED})


def generate_ticket_no() -> str:
    return f"AS{utcnow():%Y%m%d}{secrets.token_hex(3).upper()}"


async def request_refund(
    session: AsyncSession,
    *,
    order_id: int,
    reason_code: str,
    amount_cents: int | None = None,
    description: str | None = None,
    order_item_id: int | None = None,
) -> AftersalesTicket:
    """申请退款。金额缺省为全额;申请即占用可退额度,防止重复申请超额。"""
    order = await session.get(Order, order_id)
    if order is None:
        raise NotFound(f"订单 {order_id} 不存在")
    if order.status not in REFUNDABLE_ORDER_STATUSES:
        raise RuleViolation(
            f"订单 {order.order_no} 当前状态 {order.status.value} 不允许申请退款,"
            f"可退款状态:{sorted(s.value for s in REFUNDABLE_ORDER_STATUSES)}"
        )
    if reason_code not in REFUND_REASON_CODES:
        raise RuleViolation(f"未知的退款原因码 {reason_code},可选值:{sorted(REFUND_REASON_CODES)}")
    if order_item_id is not None and all(item.id != order_item_id for item in order.items):
        raise RuleViolation(f"订单明细 {order_item_id} 不属于订单 {order.order_no}")

    already_refunded_cents = await refunded_cents(session, order_id)
    already_claimed_cents = await pending_refund_cents(session, order_id)
    refundable_cents = order.total_amount_cents - already_refunded_cents
    available_cents = refundable_cents - already_claimed_cents

    # 先判额度再用额度:额度已满时给出「为什么没额度」,而不是一句金额必须为正数
    if available_cents <= 0:
        raise RuleViolation(
            f"订单 {order.order_no} 当前无可退额度:"
            f"总额 {order.total_amount_cents} 分,"
            f"已退 {already_refunded_cents} 分,"
            f"审批中 {already_claimed_cents} 分",
            order_id=order_id,
            total_amount_cents=order.total_amount_cents,
            refunded_cents=already_refunded_cents,
            claimed_cents=already_claimed_cents,
        )

    requested_cents = refundable_cents if amount_cents is None else amount_cents
    if requested_cents <= 0:
        raise RuleViolation(f"退款金额必须为正数,收到 {requested_cents}")
    if requested_cents > available_cents:
        raise RuleViolation(
            f"退款金额 {requested_cents} 分超过可退额度 {available_cents} 分"
            f"(订单总额 {order.total_amount_cents} 分,"
            f"已退 {already_refunded_cents} 分,审批中 {already_claimed_cents} 分)"
        )

    ticket = AftersalesTicket(
        ticket_no=generate_ticket_no(),
        order_id=order_id,
        order_item_id=order_item_id,
        customer_id=order.customer_id,
        type=TicketType.REFUND,
        status=TicketStatus.PENDING,
        reason_code=reason_code,
        description=description,
        refund_amount_cents=requested_cents,
        requested_at=utcnow(),
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def get_ticket(
    session: AsyncSession, ticket_id: int, *, for_update: bool = False
) -> AftersalesTicket:
    statement = select(AftersalesTicket).where(AftersalesTicket.id == ticket_id)
    if for_update:
        statement = statement.with_for_update()

    ticket = await session.scalar(statement)
    if ticket is None:
        raise NotFound(f"售后工单 {ticket_id} 不存在")
    return ticket


async def approve_ticket(
    session: AsyncSession, ticket_id: int, *, operator: str
) -> AftersalesTicket:
    ticket = await get_ticket(session, ticket_id, for_update=True)
    assert_transition("售后工单", ticket.status, TicketStatus.APPROVED, TICKET_TRANSITIONS)

    ticket.status = TicketStatus.APPROVED
    ticket.approved_at = utcnow()
    ticket.handled_by = operator
    await session.flush()
    return ticket


async def reject_ticket(
    session: AsyncSession, ticket_id: int, *, operator: str, reason: str
) -> AftersalesTicket:
    ticket = await get_ticket(session, ticket_id, for_update=True)
    assert_transition("售后工单", ticket.status, TicketStatus.REJECTED, TICKET_TRANSITIONS)

    ticket.status = TicketStatus.REJECTED
    ticket.rejected_at = utcnow()
    ticket.handled_by = operator
    ticket.reject_reason = reason
    await session.flush()
    return ticket


async def execute_refund(
    session: AsyncSession, ticket_id: int, *, operator: str
) -> AftersalesTicket:
    """执行退款。真实系统这里会调支付网关;本项目只落状态,由支付网关适配层负责外呼。"""
    ticket = await get_ticket(session, ticket_id, for_update=True)
    assert_transition("售后工单", ticket.status, TicketStatus.REFUNDED, TICKET_TRANSITIONS)

    ticket.status = TicketStatus.REFUNDED
    ticket.refunded_at = utcnow()
    ticket.handled_by = operator
    await session.flush()
    return ticket


async def close_ticket(session: AsyncSession, ticket_id: int, *, operator: str) -> AftersalesTicket:
    ticket = await get_ticket(session, ticket_id, for_update=True)
    assert_transition("售后工单", ticket.status, TicketStatus.CLOSED, TICKET_TRANSITIONS)

    ticket.status = TicketStatus.CLOSED
    ticket.closed_at = utcnow()
    ticket.handled_by = operator
    await session.flush()
    return ticket


async def refunded_cents(session: AsyncSession, order_id: int) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(AftersalesTicket.refund_amount_cents), 0)).where(
            AftersalesTicket.order_id == order_id,
            AftersalesTicket.status == TicketStatus.REFUNDED,
        )
    )
    return int(total or 0)


async def pending_refund_cents(session: AsyncSession, order_id: int) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(AftersalesTicket.refund_amount_cents), 0)).where(
            AftersalesTicket.order_id == order_id,
            AftersalesTicket.status.in_(PENDING_REFUND_STATUSES),
        )
    )
    return int(total or 0)


def ticket_requested_at(ticket: AftersalesTicket) -> datetime:
    return ticket.requested_at
