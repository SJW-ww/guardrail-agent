"""状态机的纯逻辑测试:不需要数据库,属于默认快速套件。"""

import pytest

from guardrail_api.db import Base
from guardrail_api.domain.errors import InvalidStateTransition
from guardrail_api.domain.state import (
    ORDER_TRANSITIONS,
    TICKET_TRANSITIONS,
    assert_transition,
    can_transition,
)
from guardrail_api.models import OrderStatus, TicketStatus


def test_all_business_tables_are_registered() -> None:
    """回归护栏:漏 import 一个模型,metadata 就会缺表,Alembic 会静默漏建。"""
    assert set(Base.metadata.tables) >= {
        "customer",
        "product",
        "inventory",
        "orders",
        "order_item",
        "aftersales_ticket",
    }


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.CREATED, OrderStatus.PAID),
        (OrderStatus.CREATED, OrderStatus.CANCELLED),
        (OrderStatus.PAID, OrderStatus.SHIPPED),
        (OrderStatus.PAID, OrderStatus.CANCELLED),
        (OrderStatus.SHIPPED, OrderStatus.COMPLETED),
    ],
)
def test_legal_order_transitions(current: OrderStatus, target: OrderStatus) -> None:
    assert can_transition(current, target, ORDER_TRANSITIONS)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.SHIPPED, OrderStatus.CANCELLED),
        (OrderStatus.SHIPPED, OrderStatus.PAID),
        (OrderStatus.COMPLETED, OrderStatus.CANCELLED),
        (OrderStatus.COMPLETED, OrderStatus.SHIPPED),
        (OrderStatus.CANCELLED, OrderStatus.PAID),
        (OrderStatus.PAID, OrderStatus.COMPLETED),
    ],
)
def test_illegal_order_transitions(current: OrderStatus, target: OrderStatus) -> None:
    assert not can_transition(current, target, ORDER_TRANSITIONS)


def test_terminal_order_states_have_no_way_out() -> None:
    assert ORDER_TRANSITIONS[OrderStatus.COMPLETED] == frozenset()
    assert ORDER_TRANSITIONS[OrderStatus.CANCELLED] == frozenset()
    assert OrderStatus.COMPLETED.is_terminal
    assert OrderStatus.CANCELLED.is_terminal
    assert not OrderStatus.PAID.is_terminal


def test_ticket_must_be_approved_before_refund() -> None:
    assert not can_transition(TicketStatus.PENDING, TicketStatus.REFUNDED, TICKET_TRANSITIONS)
    assert can_transition(TicketStatus.PENDING, TicketStatus.APPROVED, TICKET_TRANSITIONS)
    assert can_transition(TicketStatus.APPROVED, TicketStatus.REFUNDED, TICKET_TRANSITIONS)


def test_rejection_message_lists_allowed_targets() -> None:
    with pytest.raises(InvalidStateTransition) as excinfo:
        assert_transition("订单", OrderStatus.SHIPPED, OrderStatus.CANCELLED, ORDER_TRANSITIONS)

    error = excinfo.value
    assert error.code == "invalid_state_transition"
    assert error.context["current"] == "SHIPPED"
    assert error.context["allowed"] == ["COMPLETED"]
    assert "SHIPPED" in error.message
    assert "COMPLETED" in error.message
