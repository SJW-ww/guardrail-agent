from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from guardrail_api.db import Base
from guardrail_api.models.enums import CustomerTier, enum_column
from guardrail_api.models.mixins import TimestampMixin


class Customer(TimestampMixin, Base):
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[CustomerTier] = mapped_column(
        enum_column(CustomerTier, "customer_tier"),
        nullable=False,
        default=CustomerTier.NORMAL,
    )
