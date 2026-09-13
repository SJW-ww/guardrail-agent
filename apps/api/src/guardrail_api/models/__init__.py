"""ORM 模型。

W1 D3-D4 落地 6 张业务表:
    customer · product · inventory · orders · order_item · aftersales_ticket
W2 追加治理表:
    agent_run · agent_step · idempotency_key · audit_log

本模块必须 import 全部模型,否则 `Base.metadata` 是空的,Alembic autogenerate
和测试里的 create_all 都会看不见表。
"""

from guardrail_api.models.aftersales import AftersalesTicket
from guardrail_api.models.catalog import Inventory, Product
from guardrail_api.models.customer import Customer
from guardrail_api.models.enums import (
    REFUND_REASON_CODES,
    AuditOutcome,
    CustomerTier,
    IdempotencyStatus,
    OrderStatus,
    ProductStatus,
    RefundReasonCode,
    RunStatus,
    StepKind,
    StepStatus,
    TicketStatus,
    TicketType,
)
from guardrail_api.models.governance import AgentRun, AgentStep, AuditLog, IdempotencyRecord
from guardrail_api.models.order import Order, OrderItem

__all__ = [
    "REFUND_REASON_CODES",
    "AftersalesTicket",
    "AgentRun",
    "AgentStep",
    "AuditLog",
    "AuditOutcome",
    "Customer",
    "CustomerTier",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "Inventory",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Product",
    "ProductStatus",
    "RefundReasonCode",
    "RunStatus",
    "StepKind",
    "StepStatus",
    "TicketStatus",
    "TicketType",
]
