"""工具层的集成验收:直接调用(绕过 LLM)、结构化返回、参数校验、只提议不动钱。"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import NotFound, RuleViolation, ToolArgumentError
from guardrail_api.models import Customer, Inventory, Order, Product, TicketStatus
from guardrail_api.services import aftersales
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine
from guardrail_api.tools import ToolContext, load_tools, registry
from guardrail_api.tools.aftersales import RefundTicketView, TicketStateView
from guardrail_api.tools.logistics import TrackingView
from guardrail_api.tools.order import OrderView

pytestmark = pytest.mark.integration

ADDRESS = Address(receiver_name="张三", receiver_phone="13800000000", address="深圳市南山区 1 号")


def _context(session: AsyncSession) -> ToolContext:
    return ToolContext(session=session, actor="agent:guardrail", run_id="run-0001")


async def _seed_order(
    session: AsyncSession, *, price_cents: int = 1000, stock: int = 10, quantity: int = 2
) -> Order:
    customer = Customer(name="张三", email="zhangsan@example.com", phone="13800000000")
    product = Product(sku="SKU-TEST-01", name="测试商品", category="demo", price_cents=price_cents)
    session.add_all([customer, product])
    await session.flush()
    session.add(Inventory(product_id=product.id, available_qty=stock, reserved_qty=0))
    await session.flush()

    return await order_service.create_order(
        session,
        customer_id=customer.id,
        lines=[OrderLine(product_id=product.id, quantity=quantity)],
        address=ADDRESS,
        order_no="SO2026000001",
    )


async def test_query_order_returns_structured_view(session: AsyncSession) -> None:
    order = await _seed_order(session)

    result = await registry.invoke("query_order", _context(session), {"order_id": order.id})

    assert isinstance(result, OrderView)
    assert result.order_no == "SO2026000001"
    assert result.status == "CREATED"
    assert result.total_amount_cents == 2000
    assert result.customer.name == "张三"
    assert [item.sku for item in result.items] == ["SKU-TEST-01"]
    assert result.model_dump(mode="json")["paid_at"] is None, "返回值必须可 JSON 序列化"


async def test_query_order_accepts_order_no(session: AsyncSession) -> None:
    await _seed_order(session)

    result = await registry.invoke("query_order", _context(session), {"order_no": "SO2026000001"})

    assert isinstance(result, OrderView)
    assert result.order_no == "SO2026000001"


async def test_query_order_rejects_both_identifiers(session: AsyncSession) -> None:
    await _seed_order(session)

    with pytest.raises(ToolArgumentError) as excinfo:
        await registry.invoke(
            "query_order",
            _context(session),
            {"order_id": 1, "order_no": "SO2026000001"},
        )

    assert excinfo.value.context["errors"][0]["field"] == "__root__"


async def test_query_order_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFound):
        await registry.invoke("query_order", _context(session), {"order_id": 999999})


async def test_query_logistics_before_shipping_is_a_fact_not_an_error(
    session: AsyncSession,
) -> None:
    order = await _seed_order(session)

    result = await registry.invoke("query_logistics", _context(session), {"order_id": order.id})

    assert isinstance(result, TrackingView)
    assert result.status == "NOT_SHIPPED"
    assert result.events == []
    assert result.note is not None


async def test_query_logistics_after_shipping_is_deterministic(session: AsyncSession) -> None:
    order = await _seed_order(session)
    await order_service.pay_order(session, order.id)
    await order_service.ship_order(session, order.id)

    first = await registry.invoke("query_logistics", _context(session), {"order_id": order.id})
    second = await registry.invoke("query_logistics", _context(session), {"order_id": order.id})

    assert isinstance(first, TrackingView)
    assert first.status in {"IN_TRANSIT", "DELIVERED"}
    assert len(first.events) >= 2
    assert first.tracking_no == second.tracking_no, "同一订单号必须得到同一条轨迹"
    assert first.events[0].status == "PICKED_UP"


async def test_create_refund_only_proposes_and_moves_no_money(session: AsyncSession) -> None:
    """核心不变量:工具执行后只多了一条待审批工单,一分钱都没动。"""
    order = await _seed_order(session)
    await order_service.pay_order(session, order.id)

    result = await registry.invoke(
        "create_refund",
        _context(session),
        {"order_id": order.id, "reason_code": "QUALITY_ISSUE"},
    )

    assert isinstance(result, RefundTicketView)
    assert result.status == TicketStatus.PENDING.value
    assert result.refund_amount_cents == 2000
    assert result.order_no == order.order_no
    assert await aftersales.refunded_cents(session, order.id) == 0


async def test_create_refund_respects_available_amount(session: AsyncSession) -> None:
    order = await _seed_order(session, price_cents=1000, quantity=1)
    await order_service.pay_order(session, order.id)

    with pytest.raises(RuleViolation) as excinfo:
        await registry.invoke(
            "create_refund",
            _context(session),
            {"order_id": order.id, "reason_code": "QUALITY_ISSUE", "amount_cents": 5000},
        )

    assert "超过可退额度" in str(excinfo.value)


async def test_create_refund_invalid_reason_code_is_a_field_error(session: AsyncSession) -> None:
    order = await _seed_order(session)
    await order_service.pay_order(session, order.id)

    with pytest.raises(ToolArgumentError) as excinfo:
        await registry.invoke(
            "create_refund",
            _context(session),
            {"order_id": order.id, "reason_code": "WHATEVER"},
        )

    assert [item["field"] for item in excinfo.value.context["errors"]] == ["reason_code"]


async def test_close_ticket_works_as_compensation(session: AsyncSession) -> None:
    order = await _seed_order(session)
    await order_service.pay_order(session, order.id)
    refund = await registry.invoke(
        "create_refund",
        _context(session),
        {"order_id": order.id, "reason_code": "NO_LONGER_NEEDED"},
    )

    result = await registry.invoke(
        "close_ticket",
        _context(session),
        {"ticket_id": refund.ticket_id, "reason": "回滚:提议被驳回"},
    )

    assert isinstance(result, TicketStateView)
    assert result.status == TicketStatus.CLOSED.value
    assert result.handled_by == "agent:guardrail", "操作者来自上下文,不由模型提供"
    assert result.closed_at is not None


async def test_no_tool_exposes_raw_sql_execution() -> None:
    """回归护栏:工具层只给受限领域能力,任何形式的 SQL 入口都不允许出现。"""
    for spec in load_tools().all():
        assert "sql" not in spec.name.lower()
        assert "sql" not in spec.description.lower()
        assert "sql" not in spec.params_model.model_json_schema()["properties"]
