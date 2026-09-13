"""业务枚举。全部用 `native_enum=False`,落库为 VARCHAR + CHECK,便于迁移。"""

import enum
from typing import Literal, get_args

from sqlalchemy import Enum as SAEnum

RefundReasonCode = Literal[
    "QUALITY_ISSUE",
    "WRONG_ITEM",
    "DAMAGED_IN_TRANSIT",
    "LATE_DELIVERY",
    "NO_LONGER_NEEDED",
]


def enum_column[EnumT: enum.Enum](enum_cls: type[EnumT], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        validate_strings=True,
    )


class OrderStatus(enum.StrEnum):
    CREATED = "CREATED"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderStatus.COMPLETED, OrderStatus.CANCELLED}


class ProductStatus(enum.StrEnum):
    ON_SALE = "ON_SALE"
    OFF_SHELF = "OFF_SHELF"


class CustomerTier(enum.StrEnum):
    NORMAL = "NORMAL"
    VIP = "VIP"


class TicketType(enum.StrEnum):
    REFUND = "REFUND"
    RETURN = "RETURN"
    EXCHANGE = "EXCHANGE"


class TicketStatus(enum.StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"


class RunStatus(enum.StrEnum):
    """一次 Agent 执行的生命周期。

    这是「可写执行层」的状态中枢:所有崩溃恢复都从这几个状态推导,
    而不是靠进程内的内存标记。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    COMPENSATED = "COMPENSATED"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.COMPENSATED}


class StepStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class StepKind(enum.StrEnum):
    EXECUTE = "EXECUTE"
    APPROVAL = "APPROVAL"
    #: 补偿(回滚)步骤。用负数 seq 与正向步骤分开:第 1 步的补偿是 -1,
    #: 这样「补偿了谁」直接读得出来,也不会和原步骤抢 (run_id, seq) 的唯一约束。
    COMPENSATE = "COMPENSATE"


class IdempotencyStatus(enum.StrEnum):
    """幂等账本的状态。IN_FLIGHT 只在事务内可见,提交时必然被改写。"""

    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AuditOutcome(enum.StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# 退款原因码的取值域。
# Literal 是唯一来源:frozenset 由它派生,工具的 JSON Schema 也由它生成 ——
# 三处(文档 / 校验 / 给模型的 schema)不会漂移。
REFUND_REASON_CODES: frozenset[str] = frozenset(get_args(RefundReasonCode))
