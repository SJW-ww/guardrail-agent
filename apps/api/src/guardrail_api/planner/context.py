"""订单解析与领域上下文。

两件事,一个立场:**主键由系统解析,不由模型生成。**

- `resolve_order` 用正则从意图里把订单捞出来,再从库里取回真实订单。
  模型永远拿不到"猜一个订单号"的机会 —— 它拿到的订单主键是我们给的。
- `build_order_context` 把"这次操作会被什么业务规则挡住"提前算出来一起交给模型:
  当前状态、已退金额、审批中金额、可退额度。

注入额度信息不只是省一次工具调用。只看到"帮我退 500 元"的模型,
除了照单全收没有别的选择;看到"可退额度 80 元"的模型才**有能力**说出
"超出额度,建议拒绝"。护栏要给模型留出正确拒绝的余地。
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import RuleViolation
from guardrail_api.models import REFUND_REASON_CODES, Order, OrderStatus
from guardrail_api.services import aftersales

ORDER_NO_PATTERN = re.compile(r"SO\d{6,}")
ORDER_ID_PATTERNS = (
    re.compile(r"(?:订单|order)\s*(?:ID|id|编号)?\s*[:#]?\s*(\d+)"),
    re.compile(r"#\s*(\d+)"),
)

# 只有这些状态的订单有钱可退,和 services.aftersales 保持同一口径
REFUNDABLE_ORDER_STATUSES = frozenset(
    {OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.COMPLETED}
)


async def resolve_order(session: AsyncSession, intent: str) -> Order:
    """从意图里解析出**真实存在**的订单。解析不到就明确报错,不做模糊猜测。"""
    order_no_match = ORDER_NO_PATTERN.search(intent)
    if order_no_match:
        order_no = order_no_match.group()
        order = await session.scalar(select(Order).where(Order.order_no == order_no))
        if order is None:
            raise RuleViolation(f"订单 {order_no} 不存在,无法生成提议")
        return order

    for pattern in ORDER_ID_PATTERNS:
        id_match = pattern.search(intent)
        if id_match is None:
            continue
        order_id = int(id_match.group(1))
        order = await session.scalar(select(Order).where(Order.id == order_id))
        if order is None:
            raise RuleViolation(f"订单 ID {order_id} 不存在,无法生成提议")
        return order

    raise RuleViolation("没能从这句话里识别出订单,请提供订单号(如 SO2026000001)或订单 ID")


async def build_order_context(session: AsyncSession, order: Order) -> dict[str, Any]:
    """把执行这个订单上写操作需要用到的业务事实一次算清。"""
    refunded = await aftersales.refunded_cents(session, order.id)
    pending = await aftersales.pending_refund_cents(session, order.id)
    refundable = order.total_amount_cents - refunded
    available = refundable - pending

    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "status": order.status.value,
        "total_amount_cents": order.total_amount_cents,
        "refunded_cents": refunded,
        "pending_refund_cents": pending,
        "available_refund_cents": available,
        "refundable": order.status in REFUNDABLE_ORDER_STATUSES and available > 0,
        "allowed_reason_codes": sorted(REFUND_REASON_CODES),
    }
