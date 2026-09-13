"""W1 D3-D4 验收:下单 → 支付 → 发货 → 申请退款 → 审批 → 退款 全链路。"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import InsufficientStock, InvalidStateTransition, RuleViolation
from guardrail_api.models import Customer, Inventory, Order, OrderStatus, Product, TicketStatus
from guardrail_api.services import aftersales
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine

pytestmark = pytest.mark.integration

ADDRESS = Address(receiver_name="张三", receiver_phone="13800000000", address="深圳市南山区 1 号")


@dataclass(slots=True)
class Fixture:
    customer: Customer
    product: Product


async def _fixture(session: AsyncSession, *, price_cents: int = 1000, stock: int = 10) -> Fixture:
    customer = Customer(name="张三", email="zhangsan@example.com", phone="13800000000")
    product = Product(sku="SKU-TEST-01", name="测试商品", category="demo", price_cents=price_cents)
    session.add_all([customer, product])
    await session.flush()

    session.add(Inventory(product_id=product.id, available_qty=stock, reserved_qty=0))
    await session.flush()
    return Fixture(customer=customer, product=product)


async def _inventory(session: AsyncSession, product_id: int) -> Inventory:
    inventory = await session.scalar(select(Inventory).where(Inventory.product_id == product_id))
    assert inventory is not None
    return inventory


async def _place_order(session: AsyncSession, fixture: Fixture, quantity: int = 2) -> Order:
    return await order_service.create_order(
        session,
        customer_id=fixture.customer.id,
        lines=[OrderLine(product_id=fixture.product.id, quantity=quantity)],
        address=ADDRESS,
        order_no="SO2026000001",
    )


async def test_full_lifecycle_from_created_to_refunded(session: AsyncSession) -> None:
    fixture = await _fixture(session, stock=10)
    order = await _place_order(session, fixture, quantity=2)

    # 下单:预占库存,钱还没扣
    assert order.status is OrderStatus.CREATED
    assert order.total_amount_cents == 2000
    inventory = await _inventory(session, fixture.product.id)
    assert (inventory.available_qty, inventory.reserved_qty) == (10, 2)

    await order_service.pay_order(session, order.id)
    assert order.status is OrderStatus.PAID
    assert order.paid_at is not None
    inventory = await _inventory(session, fixture.product.id)
    assert (inventory.available_qty, inventory.reserved_qty) == (10, 2), "支付不改变库存占用"

    await order_service.ship_order(session, order.id)
    assert order.status is OrderStatus.SHIPPED
    inventory = await _inventory(session, fixture.product.id)
    assert (inventory.available_qty, inventory.reserved_qty) == (8, 0), "发货才对库存实扣"

    await order_service.complete_order(session, order.id)
    assert order.status is OrderStatus.COMPLETED

    ticket = await aftersales.request_refund(
        session, order_id=order.id, reason_code="QUALITY_ISSUE"
    )
    assert ticket.status is TicketStatus.PENDING
    assert ticket.refund_amount_cents == 2000, "不传金额默认全额退"

    await aftersales.approve_ticket(session, ticket.id, operator="supervisor-01")
    assert ticket.status is TicketStatus.APPROVED
    assert ticket.approved_at is not None

    await aftersales.execute_refund(session, ticket.id, operator="finance-01")
    assert ticket.status is TicketStatus.REFUNDED
    assert ticket.refunded_at is not None
    assert await aftersales.refunded_cents(session, order.id) == 2000


async def test_cancel_shipped_order_is_rejected(session: AsyncSession) -> None:
    fixture = await _fixture(session)
    order = await _place_order(session, fixture)
    await order_service.pay_order(session, order.id)
    await order_service.ship_order(session, order.id)

    with pytest.raises(InvalidStateTransition) as excinfo:
        await order_service.cancel_order(session, order.id, reason="客户不要了")

    message = str(excinfo.value)
    assert "SHIPPED" in message
    assert "CANCELLED" in message
    assert "允许的目标状态:COMPLETED" in message, "拒绝必须说清还能做什么"
    assert order.status is OrderStatus.SHIPPED, "失败后状态不能被改坏"


async def test_cancel_releases_reserved_stock(session: AsyncSession) -> None:
    fixture = await _fixture(session, stock=10)
    order = await _place_order(session, fixture, quantity=3)
    assert (await _inventory(session, fixture.product.id)).reserved_qty == 3

    await order_service.cancel_order(session, order.id, reason="客户取消")

    inventory = await _inventory(session, fixture.product.id)
    assert inventory.reserved_qty == 0
    assert inventory.available_qty == 10
    assert order.cancelled_at is not None


async def test_insufficient_stock_is_rejected(session: AsyncSession) -> None:
    fixture = await _fixture(session, stock=1)

    with pytest.raises(InsufficientStock) as excinfo:
        await _place_order(session, fixture, quantity=2)

    assert "可用库存不足" in str(excinfo.value)


async def test_refund_cannot_exceed_order_total(session: AsyncSession) -> None:
    fixture = await _fixture(session, price_cents=1000)
    order = await _place_order(session, fixture, quantity=1)
    await order_service.pay_order(session, order.id)

    with pytest.raises(RuleViolation) as excinfo:
        await aftersales.request_refund(
            session, order_id=order.id, reason_code="QUALITY_ISSUE", amount_cents=9999
        )

    assert "超过可退额度" in str(excinfo.value)


async def test_second_full_refund_is_rejected(session: AsyncSession) -> None:
    fixture = await _fixture(session, price_cents=1000)
    order = await _place_order(session, fixture, quantity=1)
    await order_service.pay_order(session, order.id)

    first = await aftersales.request_refund(session, order_id=order.id, reason_code="QUALITY_ISSUE")
    await aftersales.approve_ticket(session, first.id, operator="supervisor-01")
    await aftersales.execute_refund(session, first.id, operator="finance-01")

    with pytest.raises(RuleViolation) as excinfo:
        await aftersales.request_refund(session, order_id=order.id, reason_code="NO_LONGER_NEEDED")

    assert "当前无可退额度" in str(excinfo.value)
    assert "已退 1000 分" in str(excinfo.value)


async def test_unknown_reason_code_is_rejected(session: AsyncSession) -> None:
    fixture = await _fixture(session)
    order = await _place_order(session, fixture)
    await order_service.pay_order(session, order.id)

    with pytest.raises(RuleViolation) as excinfo:
        await aftersales.request_refund(session, order_id=order.id, reason_code="NOT_A_REASON")

    assert "未知的退款原因码" in str(excinfo.value)


async def test_refund_requires_paid_order(session: AsyncSession) -> None:
    fixture = await _fixture(session)
    order = await _place_order(session, fixture)

    with pytest.raises(RuleViolation):
        await aftersales.request_refund(session, order_id=order.id, reason_code="QUALITY_ISSUE")


async def test_update_address_returns_before_and_after(session: AsyncSession) -> None:
    fixture = await _fixture(session)
    order = await _place_order(session, fixture)

    changes = await order_service.update_address(
        session,
        order.id,
        Address(receiver_name="李四", receiver_phone="13900000000", address="东莞市松山湖 8 栋"),
    )

    assert {change.field for change in changes} == {"receiver_name", "receiver_phone", "address"}
    assert order.receiver_name == "李四"

    await order_service.pay_order(session, order.id)
    await order_service.ship_order(session, order.id)
    with pytest.raises(RuleViolation) as excinfo:
        await order_service.update_address(session, order.id, ADDRESS)
    assert "不允许修改收货地址" in str(excinfo.value)
