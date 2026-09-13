"""状态机。

业务规则用**显式转移表**表达,而不是散落在 if 里:
数据库有 CHECK 约束兜底,这里负责给出「为什么不行」的可读理由。
"""

from collections.abc import Mapping
from enum import StrEnum

from guardrail_api.domain.errors import InvalidStateTransition
from guardrail_api.models.enums import OrderStatus, TicketStatus

# 订单:CREATED → PAID → SHIPPED → COMPLETED;CANCELLED 只能从 CREATED / PAID 进
ORDER_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.PAID, OrderStatus.CANCELLED}),
    OrderStatus.PAID: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.COMPLETED}),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

# 售后工单:PENDING → APPROVED → REFUNDED;或 PENDING → REJECTED / CLOSED
TICKET_TRANSITIONS: Mapping[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.PENDING: frozenset(
        {TicketStatus.APPROVED, TicketStatus.REJECTED, TicketStatus.CLOSED}
    ),
    TicketStatus.APPROVED: frozenset({TicketStatus.REFUNDED}),
    TicketStatus.REJECTED: frozenset(),
    TicketStatus.REFUNDED: frozenset(),
    TicketStatus.CLOSED: frozenset(),
}


def allowed_transitions[StateT: StrEnum](
    current: StateT, transitions: Mapping[StateT, frozenset[StateT]]
) -> frozenset[StateT]:
    return transitions.get(current, frozenset())


def can_transition[StateT: StrEnum](
    current: StateT, target: StateT, transitions: Mapping[StateT, frozenset[StateT]]
) -> bool:
    return target in allowed_transitions(current, transitions)


def assert_transition[StateT: StrEnum](
    entity: str,
    current: StateT,
    target: StateT,
    transitions: Mapping[StateT, frozenset[StateT]],
) -> None:
    """不合法就抛错,理由里带上允许的目标状态 —— 拒绝也要讲清楚为什么。"""
    if can_transition(current, target, transitions):
        return

    allowed = sorted(state.value for state in allowed_transitions(current, transitions))
    hint = "、".join(allowed) if allowed else "无(已是终态)"
    raise InvalidStateTransition(
        f"{entity}当前状态 {current.value} 不允许流转到 {target.value},允许的目标状态:{hint}",
        entity=entity,
        current=current.value,
        target=target.value,
        allowed=allowed,
    )
