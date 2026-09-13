from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from guardrail_api.domain.errors import NotFound
from guardrail_api.models import CustomerTier, Order, OrderStatus
from guardrail_api.tools.base import RiskLevel, ToolContext
from guardrail_api.tools.registry import register


class QueryOrderParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int | None = Field(default=None, ge=1, description="订单 ID,与 order_no 二选一")
    order_no: str | None = Field(
        default=None, min_length=6, max_length=32, description="订单号,与 order_id 二选一"
    )

    @model_validator(mode="after")
    def _require_exactly_one(self) -> "QueryOrderParams":
        if (self.order_id is None) == (self.order_no is None):
            raise ValueError("order_id 与 order_no 必须且只能提供一个")
        return self


class CustomerView(BaseModel):
    id: int
    name: str
    tier: CustomerTier


class ReceiverView(BaseModel):
    name: str
    phone: str
    address: str


class OrderItemView(BaseModel):
    order_item_id: int
    product_id: int
    sku: str
    name: str
    quantity: int
    unit_price_cents: int
    amount_cents: int


class OrderView(BaseModel):
    order_id: int
    order_no: str
    status: OrderStatus
    total_amount_cents: int
    currency: str
    customer: CustomerView
    receiver: ReceiverView
    items: list[OrderItemView]
    created_at: datetime
    paid_at: datetime | None = None
    shipped_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


def to_order_view(order: Order) -> OrderView:
    return OrderView(
        order_id=order.id,
        order_no=order.order_no,
        status=order.status,
        total_amount_cents=order.total_amount_cents,
        currency=order.currency,
        customer=CustomerView(
            id=order.customer.id, name=order.customer.name, tier=order.customer.tier
        ),
        receiver=ReceiverView(
            name=order.receiver_name, phone=order.receiver_phone, address=order.address
        ),
        items=[
            OrderItemView(
                order_item_id=item.id,
                product_id=item.product_id,
                sku=item.product.sku,
                name=item.product.name,
                quantity=item.quantity,
                unit_price_cents=item.unit_price_cents,
                amount_cents=item.amount_cents,
            )
            for item in order.items
        ],
        created_at=order.created_at,
        paid_at=order.paid_at,
        shipped_at=order.shipped_at,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
        cancel_reason=order.cancel_reason,
    )


async def load_order(context: ToolContext, order_id: int | None, order_no: str | None) -> Order:
    """按 ID 或订单号取订单 —— 两个只读工具共用,取不到就明确报错。"""
    session = context.session
    # 统一走 select 而不是 session.get:
    # get() 命中同一会话里刚创建、尚未加载关系的对象时不会触发 selectin 加载,
    # 之后访问 item.product 会在非 await 上下文里发起 IO,抛 MissingGreenlet。
    # 只有一条查询路径,行为才可预测。
    if order_id is not None:
        statement = select(Order).where(Order.id == order_id)
    else:
        statement = select(Order).where(Order.order_no == order_no)

    order = await session.scalar(statement)
    if order is None:
        identifier = f"ID {order_id}" if order_id is not None else f"订单号 {order_no}"
        raise NotFound(f"订单 {identifier} 不存在")
    return order


@register(
    name="query_order",
    title="查询订单",
    description=(
        "按订单 ID 或订单号查询订单的完整状态:订单状态、金额、客户与收货信息、商品明细、"
        "各个关键时间点。只读,不产生任何副作用。"
    ),
    risk_level=RiskLevel.READ_ONLY,
    params_model=QueryOrderParams,
    result_model=OrderView,
    tags=("order", "read"),
)
async def query_order(context: ToolContext, params: QueryOrderParams) -> OrderView:
    order = await load_order(context, params.order_id, params.order_no)
    return to_order_view(order)
