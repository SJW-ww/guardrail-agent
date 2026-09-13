"""确定性规划器:规则解析意图 → 结构化提议。不调用任何模型。"""

import re
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import RuleViolation
from guardrail_api.models import Order
from guardrail_api.tools import load_tools

ORDER_NO_PATTERN = re.compile(r"SO\d{6,}")
NUMBER_PATTERN = re.compile(r"(\d+)\s*(?:元|块|块钱)")

REASON_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("质量", "坏了", "故障", "不能用"), "QUALITY_ISSUE"),
    (("发错", "错货", "不是这个"), "WRONG_ITEM"),
    (("破损", "损坏", "摔", "裂"), "DAMAGED_IN_TRANSIT"),
    (("太慢", "延迟", "晚到", "没到"), "LATE_DELIVERY"),
    (("不想要", "不需要", "后悔", "买错"), "NO_LONGER_NEEDED"),
)

INTENT_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    # 覆盖真实说法:中文里「帮我退 80 元」比「申请退款」更常见,所以必须带单字「退」
    (("退款", "退钱", "退货", "退"), "create_refund"),
    (("物流", "快递", "到哪", "运单", "签收"), "query_logistics"),
    (("查订单", "订单详情", "看看订单", "查询订单"), "query_order"),
)


class Proposal(BaseModel):
    """模型唯一被允许的输出形态:结构化提议,不是 SQL,也不是自由文本。"""

    action: str = Field(description="要调用的工具名")
    arguments: dict[str, Any] = Field(description="工具入参")
    rationale: str = Field(description="为什么这么做,给审批人看")
    evidence: list[str] = Field(description="依据的事实")
    risk_level: str
    requires_approval: bool = Field(description="是否必须人工审批后才执行")
    confidence: float = Field(ge=0, le=1)


def _match(keyword_groups: tuple[tuple[tuple[str, ...], str], ...], text: str) -> str | None:
    for keywords, value in keyword_groups:
        if any(keyword in text for keyword in keywords):
            return value
    return None


async def _resolve_order(session: AsyncSession, intent: str) -> tuple[Order, str]:
    order_no_match = ORDER_NO_PATTERN.search(intent)
    if order_no_match:
        order = await session.scalar(select(Order).where(Order.order_no == order_no_match.group()))
        if order is None:
            raise RuleViolation(f"订单 {order_no_match.group()} 不存在,无法生成提议")
        return order, f"订单号 {order.order_no}"

    id_match = re.search(r"(?:订单|order)\s*#?(\d+)", intent, re.IGNORECASE)
    if id_match:
        order_id = int(id_match.group(1))
        order = await session.scalar(select(Order).where(Order.id == order_id))
        if order is None:
            raise RuleViolation(f"订单 ID {order_id} 不存在,无法生成提议")
        return order, f"订单 ID {order.id}"

    raise RuleViolation("没能从这句话里识别出订单,请提供订单号或订单 ID")


async def draft(session: AsyncSession, intent: str) -> Proposal:
    """把一句自然语言变成一条可校验、可展示、可审批的提议。"""
    tools = load_tools()
    action = _match(INTENT_KEYWORDS, intent)
    if action is None:
        raise RuleViolation("没能识别出可执行的操作,目前支持:退款申请、物流查询、订单查询")

    spec = tools.get(action)
    order, evidence = await _resolve_order(session, intent)
    arguments: dict = {"order_id": order.id}
    rationale_parts = [f"针对{evidence}(当前状态 {order.status.value})"]

    if action == "create_refund":
        reason_code = _match(REASON_KEYWORDS, intent)
        if reason_code is None:
            raise RuleViolation("申请退款需要说明原因,例如:质量问题 / 发错货 / 运输破损 / 到货太慢")
        arguments["reason_code"] = reason_code

        amount_match = NUMBER_PATTERN.search(intent)
        if amount_match:
            amount_cents = int(amount_match.group(1)) * 100
            arguments["amount_cents"] = amount_cents
            rationale_parts.append(f"退款金额 {amount_cents} 分")
        else:
            rationale_parts.append("金额未指定,按可退额度全额申请")
        rationale_parts.append(f"原因码 {reason_code}")

    return Proposal(
        action=action,
        arguments=arguments,
        rationale=";".join(rationale_parts),
        evidence=[evidence, f"订单金额 {order.total_amount_cents} 分"],
        risk_level=spec.risk_level.value,
        requires_approval=not spec.is_read_only,
        confidence=0.9,
    )
