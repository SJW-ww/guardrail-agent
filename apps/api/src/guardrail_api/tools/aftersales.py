from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from guardrail_api.domain.errors import NotFound
from guardrail_api.models import AftersalesTicket, Order, RefundReasonCode, TicketStatus
from guardrail_api.services import aftersales
from guardrail_api.tools.base import CompensateArg, RiskLevel, ToolContext
from guardrail_api.tools.order import load_order
from guardrail_api.tools.registry import register


class CreateRefundParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(ge=1, description="要退款的订单 ID")
    reason_code: RefundReasonCode = Field(description="退款原因码,取值域见 JSON Schema 的 enum")
    amount_cents: int | None = Field(
        default=None, ge=1, description="退款金额(分);不传表示全额退款"
    )
    description: str | None = Field(default=None, max_length=500, description="补充说明")


class RefundTicketView(BaseModel):
    ticket_id: int
    ticket_no: str
    order_no: str
    status: TicketStatus
    reason_code: str
    refund_amount_cents: int | None
    requested_at: datetime
    note: str


class QueryRefundableParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int = Field(ge=1, description="要查询的订单 ID")


class RefundableView(BaseModel):
    """一张订单此刻的退款额度账。**只读**,但它经常是多步计划的第 1 步:
    后面的退款金额要引用这里的 available_refund_cents,而不是让写操作自己去猜。"""

    order_id: int
    order_no: str
    status: str
    total_amount_cents: int
    refunded_cents: int
    pending_refund_cents: int
    available_refund_cents: int


class CloseTicketParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: int = Field(ge=1, description="售后工单 ID")
    reason: str = Field(min_length=1, max_length=200, description="关闭原因")


class TicketStateView(BaseModel):
    ticket_id: int
    ticket_no: str
    status: TicketStatus
    handled_by: str | None = None
    closed_at: datetime | None = None


def _ticket_snapshot(ticket: AftersalesTicket) -> dict[str, Any]:
    return {
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "status": ticket.status.value,
        "refund_amount_cents": ticket.refund_amount_cents,
        "refunded_at": ticket.refunded_at.isoformat() if ticket.refunded_at else None,
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
        "handled_by": ticket.handled_by,
    }


async def refund_snapshot(context: ToolContext, params: CreateRefundParams) -> dict[str, Any]:
    """退款会动的世界:订单的额度 + 这张订单下已有的全部工单。

    只盯这两样 —— 客户地址、商品库存跟这次操作没关系,塞进审计只会让人找不着重点。
    """
    order = await context.session.get(Order, params.order_id)
    if order is None:
        raise NotFound(f"订单 {params.order_id} 不存在")

    tickets = (
        (
            await context.session.execute(
                select(AftersalesTicket)
                .where(AftersalesTicket.order_id == order.id)
                .order_by(AftersalesTicket.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "order": {
            "order_id": order.id,
            "order_no": order.order_no,
            "status": order.status.value,
            "total_amount_cents": order.total_amount_cents,
        },
        "tickets": [_ticket_snapshot(ticket) for ticket in tickets],
    }


async def refund_amount(context: ToolContext, params: CreateRefundParams) -> int | None:
    """这笔退款实际要动多少钱。金额不传 = 按可退额度全额退,那就去订单上算出来。

    治理层要的是「这次操作动多少钱」,而它不会去解析业务规则 —— 规则写在这里。
    """
    if params.amount_cents is not None:
        return params.amount_cents
    return await aftersales.available_refund_cents(context.session, params.order_id)


async def ticket_snapshot(context: ToolContext, params: CloseTicketParams) -> dict[str, Any]:
    ticket = await aftersales.get_ticket(context.session, params.ticket_id)
    return {"ticket": _ticket_snapshot(ticket)}


@register(
    name="create_refund",
    title="发起退款申请",
    description=(
        "为订单创建退款申请工单,进入待审批状态。**不会直接退款、不产生资金变动**;"
        "金额不传表示按可退额度全额申请,超过可退额度会被拒绝。"
    ),
    risk_level=RiskLevel.HIGH,
    params_model=CreateRefundParams,
    result_model=RefundTicketView,
    preconditions=(
        "订单状态属于 PAID / SHIPPED / COMPLETED",
        "申请金额不超过订单可退额度",
        "退款原因码必须在取值域内",
    ),
    side_effect="创建一条 PENDING 状态的售后工单(未退款)",
    idempotent=True,
    idempotency_key="hash(run_uid, tool, args)",
    compensate_tool="close_ticket",
    # 补偿参数从原步骤的产出里取:退款工单的 ticket_id 是这次写入产生的,
    # 只有它才能精确地把这一条关掉(而不是"关掉这张订单上的某个工单")。
    compensate_args={
        "ticket_id": CompensateArg(result_path="ticket_id"),
        "reason": CompensateArg(const="计划未完成,系统按声明撤销这条退款申请"),
    },
    snapshot=refund_snapshot,
    reason_field="description",
    amount_field="amount_cents",
    amount_resolver=refund_amount,
    tags=("aftersales", "write", "money"),
)
async def create_refund(context: ToolContext, params: CreateRefundParams) -> RefundTicketView:
    order = await load_order(context, params.order_id, None)

    ticket = await aftersales.request_refund(
        context.session,
        order_id=order.id,
        reason_code=params.reason_code,
        amount_cents=params.amount_cents,
        description=params.description,
    )

    return RefundTicketView(
        ticket_id=ticket.id,
        ticket_no=ticket.ticket_no,
        order_no=order.order_no,
        status=ticket.status,
        reason_code=ticket.reason_code,
        refund_amount_cents=ticket.refund_amount_cents,
        requested_at=ticket.requested_at,
        note=(
            f"已创建待审批退款工单 {ticket.ticket_no},金额 {ticket.refund_amount_cents} 分。"
            "审批通过后才会实际退款。"
        ),
    )


@register(
    name="query_refundable",
    title="查询可退额度",
    description=(
        "查一张订单此刻还能退多少钱:订单总额、已退金额、审批中占用的额度、剩余可退额度。"
        "只读,不产生任何副作用。需要按可退额度退款时,先用它把金额查出来。"
    ),
    risk_level=RiskLevel.READ_ONLY,
    params_model=QueryRefundableParams,
    result_model=RefundableView,
    tags=("aftersales", "read", "money"),
)
async def query_refundable(context: ToolContext, params: QueryRefundableParams) -> RefundableView:
    order = await load_order(context, params.order_id, None)
    refunded = await aftersales.refunded_cents(context.session, order.id)
    pending = await aftersales.pending_refund_cents(context.session, order.id)
    return RefundableView(
        order_id=order.id,
        order_no=order.order_no,
        status=order.status.value,
        total_amount_cents=order.total_amount_cents,
        refunded_cents=refunded,
        pending_refund_cents=pending,
        available_refund_cents=order.total_amount_cents - refunded - pending,
    )


@register(
    name="close_ticket",
    title="关闭售后工单",
    description="关闭一条待处理的售后工单。也作为 create_refund 的补偿动作使用。",
    risk_level=RiskLevel.LOW,
    params_model=CloseTicketParams,
    result_model=TicketStateView,
    preconditions=("工单处于 PENDING 状态",),
    side_effect="把售后工单置为 CLOSED",
    idempotent=True,
    idempotency_key="hash(run_uid, tool, args)",
    snapshot=ticket_snapshot,
    reason_field="reason",
    tags=("aftersales", "write"),
)
async def close_ticket(context: ToolContext, params: CloseTicketParams) -> TicketStateView:
    ticket = await aftersales.get_ticket(context.session, params.ticket_id)
    closed = await aftersales.close_ticket(context.session, ticket.id, operator=context.actor)
    return TicketStateView(
        ticket_id=closed.id,
        ticket_no=closed.ticket_no,
        status=closed.status,
        handled_by=closed.handled_by,
        closed_at=closed.closed_at,
    )
