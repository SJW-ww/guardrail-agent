"""订单领域服务。每条业务规则是一个显式方法,不塞进 API 路由。"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.clock import utcnow
from guardrail_api.domain.errors import InsufficientStock, NotFound, RuleViolation
from guardrail_api.domain.state import ORDER_TRANSITIONS, assert_transition
from guardrail_api.models import (
    Customer,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductStatus,
)


@dataclass(frozen=True, slots=True)
class OrderLine:
    product_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class Address:
    receiver_name: str
    receiver_phone: str
    address: str


@dataclass(frozen=True, slots=True)
class FieldChange:
    """一次字段变更的前后值 —— 审计表的原料。"""

    field: str
    before: str
    after: str


def generate_order_no() -> str:
    return f"SO{utcnow():%Y%m%d}{secrets.token_hex(3).upper()}"


async def create_order(
    session: AsyncSession,
    *,
    customer_id: int,
    lines: Sequence[OrderLine],
    address: Address,
    order_no: str | None = None,
) -> Order:
    """下单:校验客户与商品 → 预占库存 → 生成订单,状态 CREATED。"""
    if not lines:
        raise RuleViolation("订单至少需要一个商品")

    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise NotFound(f"客户 {customer_id} 不存在")

    order = Order(
        order_no=order_no or generate_order_no(),
        # 用关系而不是外键:对象已经在手上,顺带避免后续查询触发懒加载
        customer=customer,
        status=OrderStatus.CREATED,
        currency="CNY",
        total_amount_cents=0,
        receiver_name=address.receiver_name,
        receiver_phone=address.receiver_phone,
        address=address.address,
    )
    session.add(order)

    total_amount_cents = 0
    for line in lines:
        product = await session.get(Product, line.product_id)
        if product is None:
            raise NotFound(f"商品 {line.product_id} 不存在")
        if line.quantity <= 0:
            raise RuleViolation(f"商品 {product.sku} 的下单数量必须为正数,收到 {line.quantity}")
        if product.status is not ProductStatus.ON_SALE:
            raise RuleViolation(f"商品 {product.sku} 已下架,不能下单")

        amount_cents = product.price_cents * line.quantity
        total_amount_cents += amount_cents
        order.items.append(
            OrderItem(
                product=product,
                quantity=line.quantity,
                unit_price_cents=product.price_cents,
                amount_cents=amount_cents,
            )
        )
        await _reserve_stock(session, product, line.quantity)

    order.total_amount_cents = total_amount_cents
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: int, *, for_update: bool = False) -> Order:
    statement = select(Order).where(Order.id == order_id)
    if for_update:
        statement = statement.with_for_update()

    order = await session.scalar(statement)
    if order is None:
        raise NotFound(f"订单 {order_id} 不存在")
    return order


async def pay_order(session: AsyncSession, order_id: int) -> Order:
    order = await get_order(session, order_id, for_update=True)
    assert_transition("订单", order.status, OrderStatus.PAID, ORDER_TRANSITIONS)

    order.status = OrderStatus.PAID
    order.paid_at = utcnow()
    await session.flush()
    return order


async def ship_order(session: AsyncSession, order_id: int) -> Order:
    """发货:预占转实扣,库存真正减少。"""
    order = await get_order(session, order_id, for_update=True)
    assert_transition("订单", order.status, OrderStatus.SHIPPED, ORDER_TRANSITIONS)

    for item in order.items:
        inventory = await _locked_inventory(session, item.product_id)
        inventory.available_qty -= item.quantity
        inventory.reserved_qty -= item.quantity
        inventory.version += 1

    order.status = OrderStatus.SHIPPED
    order.shipped_at = utcnow()
    await session.flush()
    return order


async def complete_order(session: AsyncSession, order_id: int) -> Order:
    order = await get_order(session, order_id, for_update=True)
    assert_transition("订单", order.status, OrderStatus.COMPLETED, ORDER_TRANSITIONS)

    order.status = OrderStatus.COMPLETED
    order.completed_at = utcnow()
    await session.flush()
    return order


async def cancel_order(session: AsyncSession, order_id: int, *, reason: str) -> Order:
    """取消订单:只有未发货的 CREATED / PAID 可以取消,并释放预占库存。

    已发货订单要走退货流程 —— 这是状态机在业务上的意义,不是随手加的校验。
    """
    order = await get_order(session, order_id, for_update=True)
    assert_transition("订单", order.status, OrderStatus.CANCELLED, ORDER_TRANSITIONS)

    for item in order.items:
        inventory = await _locked_inventory(session, item.product_id)
        inventory.reserved_qty -= item.quantity
        inventory.version += 1

    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    order.cancel_reason = reason
    await session.flush()
    return order


async def update_address(
    session: AsyncSession, order_id: int, address: Address
) -> list[FieldChange]:
    """改收货地址:只能改未发货的订单,返回前后值供审计与补偿。"""
    order = await get_order(session, order_id, for_update=True)
    if order.status not in {OrderStatus.CREATED, OrderStatus.PAID}:
        raise RuleViolation(
            f"订单 {order.order_no} 当前状态 {order.status.value} 不允许修改收货地址,"
            "只有未发货的订单可以修改"
        )

    changes: list[FieldChange] = []
    for field, new_value in (
        ("receiver_name", address.receiver_name),
        ("receiver_phone", address.receiver_phone),
        ("address", address.address),
    ):
        before = getattr(order, field)
        if before != new_value:
            changes.append(FieldChange(field=field, before=str(before), after=str(new_value)))
            setattr(order, field, new_value)

    await session.flush()
    return changes


async def _locked_inventory(session: AsyncSession, product_id: int) -> Inventory:
    """取库存行并加行锁 —— 并发下单时靠它串行化,而不是靠应用层判断。"""
    inventory = await session.scalar(
        select(Inventory).where(Inventory.product_id == product_id).with_for_update()
    )
    if inventory is None:
        raise NotFound(f"商品 {product_id} 缺少库存记录")
    return inventory


async def _reserve_stock(session: AsyncSession, product: Product, quantity: int) -> None:
    inventory = await _locked_inventory(session, product.id)
    if inventory.sellable_qty < quantity:
        raise InsufficientStock(
            f"商品 {product.sku} 可用库存不足:需要 {quantity},可用 {inventory.sellable_qty}",
            product_id=product.id,
            required=quantity,
            sellable=inventory.sellable_qty,
        )
    inventory.reserved_qty += quantity
    inventory.version += 1


def order_paid_at(order: Order) -> datetime | None:
    return order.paid_at
