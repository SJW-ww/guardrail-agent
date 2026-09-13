from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from guardrail_api.db import Base
from guardrail_api.models.enums import TicketStatus, TicketType, enum_column
from guardrail_api.models.mixins import TimestampMixin


class AftersalesTicket(TimestampMixin, Base):
    """售后工单。金额与状态流转是它的核心,备注与描述只是辅助信息。"""

    __tablename__ = "aftersales_ticket"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("order_item.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customer.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    type: Mapped[TicketType] = mapped_column(enum_column(TicketType, "ticket_type"), nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        enum_column(TicketStatus, "ticket_status"),
        nullable=False,
        default=TicketStatus.PENDING,
        index=True,
    )
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    refund_amount_cents: Mapped[int | None] = mapped_column(BigInteger)

    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_by: Mapped[str | None] = mapped_column(String(64))
    reject_reason: Mapped[str | None] = mapped_column(String(256))
