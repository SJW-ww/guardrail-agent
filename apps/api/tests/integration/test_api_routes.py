"""REST 层的集成验收。前端与端到端测试消费的就是这些契约。"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.models import Customer, Inventory, Order, Product
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine

pytestmark = pytest.mark.integration

ADDRESS = Address(receiver_name="张三", receiver_phone="13800000000", address="深圳市南山区 1 号")


async def _seed_order(
    session: AsyncSession,
    *,
    index: int = 1,
    price_cents: int = 1000,
    quantity: int = 2,
    stock: int = 10,
) -> Order:
    customer = Customer(
        name=f"客户{index:03d}", email=f"customer{index:03d}@example.com", phone="13800000000"
    )
    product = Product(
        sku=f"SKU-{index:04d}", name=f"商品{index:04d}", category="demo", price_cents=price_cents
    )
    session.add_all([customer, product])
    await session.flush()
    session.add(Inventory(product_id=product.id, available_qty=stock, reserved_qty=0))
    await session.flush()

    order = await order_service.create_order(
        session,
        customer_id=customer.id,
        lines=[OrderLine(product_id=product.id, quantity=quantity)],
        address=ADDRESS,
        order_no=f"SO2026{index:06d}",
    )
    # 路由用的是另一个会话,必须提交后才可见
    await session.commit()
    return order


async def test_order_list_returns_seeded_rows(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    await _seed_order(session, index=1)
    await _seed_order(session, index=2, price_cents=2500, quantity=1)

    response = await api_client.get("/api/orders", params={"limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 10
    assert {item["order_no"] for item in body["items"]} == {"SO2026000001", "SO2026000002"}
    assert body["items"][0]["status"] == "CREATED"
    assert body["items"][0]["customer_name"].startswith("客户")


async def test_order_list_filters_by_status(session: AsyncSession, api_client: AsyncClient) -> None:
    order = await _seed_order(session, index=1)
    await order_service.pay_order(session, order.id)
    await _seed_order(session, index=2)
    await session.commit()

    response = await api_client.get("/api/orders", params={"status": "PAID"})

    assert response.status_code == 200
    assert [item["order_no"] for item in response.json()["items"]] == ["SO2026000001"]


async def test_order_detail_not_found_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/orders/999999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_order_lifecycle_via_api(session: AsyncSession, api_client: AsyncClient) -> None:
    order = await _seed_order(session)

    for action, expected in (("pay", "PAID"), ("ship", "SHIPPED"), ("complete", "COMPLETED")):
        response = await api_client.post(f"/api/orders/{order.id}/{action}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected

    detail = await api_client.get(f"/api/orders/{order.id}")
    assert detail.json()["completed_at"] is not None


async def test_cancel_shipped_order_returns_409(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    order = await _seed_order(session)
    await api_client.post(f"/api/orders/{order.id}/pay")
    await api_client.post(f"/api/orders/{order.id}/ship")

    response = await api_client.post(
        f"/api/orders/{order.id}/cancel", json={"reason": "客户不要了"}
    )

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "invalid_state_transition"
    assert "允许的目标状态:COMPLETED" in body["message"]


async def test_tools_endpoint_exposes_declarations(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/tools")

    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()}
    assert set(tools) == {"query_order", "query_logistics", "create_refund", "close_ticket"}
    assert tools["create_refund"]["risk_level"] == "high"
    assert tools["create_refund"]["compensate_tool"] == "close_ticket"
    assert tools["create_refund"]["parameters"]["properties"]["reason_code"]["enum"]
    assert tools["query_order"]["read_only"] is True


async def test_invalid_tool_arguments_return_422_with_field_errors(
    api_client: AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/tools/query_order/invoke", json={"arguments": {"order_id": 0}}
    )

    assert response.status_code == 422
    errors = response.json()["error"]["context"]["errors"]
    assert [item["field"] for item in errors] == ["order_id"]


async def test_tool_invocation_creates_pending_ticket_without_moving_money(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    order = await _seed_order(session)
    await api_client.post(f"/api/orders/{order.id}/pay")

    response = await api_client.post(
        "/api/tools/create_refund/invoke",
        json={"arguments": {"order_id": order.id, "reason_code": "QUALITY_ISSUE"}},
        headers={"X-Actor": "operator-07"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actor"] == "human:operator-07", "操作者来自身份,不由请求体提供"
    assert body["result"]["status"] == "PENDING"
    assert body["result"]["refund_amount_cents"] == 2000


async def test_approval_flow_moves_ticket_to_refunded(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    order = await _seed_order(session)
    await api_client.post(f"/api/orders/{order.id}/pay")
    await api_client.post(
        "/api/tools/create_refund/invoke",
        json={"arguments": {"order_id": order.id, "reason_code": "WRONG_ITEM"}},
    )

    pending = await api_client.get("/api/tickets", params={"status": "PENDING"})
    assert pending.status_code == 200
    ticket = pending.json()["items"][0]
    assert ticket["order_no"] == "SO2026000001"

    approved = await api_client.post(
        f"/api/tickets/{ticket['ticket_id']}/approve", headers={"X-Actor": "supervisor-01"}
    )
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["handled_by"] == "supervisor-01"

    refunded = await api_client.post(
        f"/api/tickets/{ticket['ticket_id']}/refund", headers={"X-Actor": "finance-01"}
    )
    assert refunded.json()["status"] == "REFUNDED"


async def test_rejecting_a_refunded_ticket_returns_409(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    order = await _seed_order(session)
    await api_client.post(f"/api/orders/{order.id}/pay")
    await api_client.post(
        "/api/tools/create_refund/invoke",
        json={"arguments": {"order_id": order.id, "reason_code": "WRONG_ITEM"}},
    )
    tickets = (await api_client.get("/api/tickets")).json()["items"]
    ticket_id = tickets[0]["ticket_id"]
    await api_client.post(f"/api/tickets/{ticket_id}/approve")
    await api_client.post(f"/api/tickets/{ticket_id}/refund")

    response = await api_client.post(f"/api/tickets/{ticket_id}/reject", json={"reason": "太晚了"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"


async def test_planner_draft_returns_structured_proposal(
    session: AsyncSession, api_client: AsyncClient
) -> None:
    await _seed_order(session)

    response = await api_client.post(
        "/api/planner/draft",
        json={"intent": "订单 SO2026000001 质量有问题,帮我退 80 元"},
    )

    assert response.status_code == 200, response.text
    proposal = response.json()
    assert proposal["action"] == "create_refund"
    assert proposal["arguments"]["reason_code"] == "QUALITY_ISSUE"
    assert proposal["arguments"]["amount_cents"] == 8000
    assert proposal["risk_level"] == "high"
    assert proposal["requires_approval"] is True
    assert proposal["evidence"]


async def test_planner_draft_rejects_unrecognized_intent(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/planner/draft", json={"intent": "今天天气不错"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "rule_violation"
