from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from guardrail_api.db import Base
from guardrail_api.models.enums import ProductStatus, enum_column
from guardrail_api.models.mixins import TimestampMixin


class Product(TimestampMixin, Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ProductStatus] = mapped_column(
        enum_column(ProductStatus, "product_status"),
        nullable=False,
        default=ProductStatus.ON_SALE,
    )


class Inventory(TimestampMixin, Base):
    """一商品一仓一行。

    `version` 是乐观锁版本号;`reserved <= available` 这条不变量直接交给数据库守,
    库存被改坏时会在写的那一刻就报错,而不是等到对账才发现。
    """

    __tablename__ = "inventory"
    __table_args__ = (
        CheckConstraint("available_qty >= 0", name="ck_inventory_available_non_negative"),
        CheckConstraint("reserved_qty >= 0", name="ck_inventory_reserved_non_negative"),
        CheckConstraint(
            "reserved_qty <= available_qty", name="ck_inventory_reserved_within_available"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    warehouse_code: Mapped[str] = mapped_column(String(32), nullable=False, default="WH-01")
    available_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    product: Mapped[Product] = relationship(lazy="selectin")

    @property
    def sellable_qty(self) -> int:
        return self.available_qty - self.reserved_qty
