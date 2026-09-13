"""确定性规划器:规则解析意图 → 结构化提议。不调用任何模型。

它的存在有两个理由,不是一个"降级方案":
- **离线可跑。** CI 与本地演示不依赖任何外部服务,地基不稳时先别急着接模型。
- **对照物。** 同一个意图同时喂给规则和模型,能直接看出"模型的贡献到底在哪"。
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import RuleViolation
from guardrail_api.models import Order
from guardrail_api.planner.context import build_order_context, resolve_order
from guardrail_api.planner.finalize import finalize
from guardrail_api.planner.proposal import Proposal
from guardrail_api.tools import load_tools

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


def _match(keyword_groups: tuple[tuple[tuple[str, ...], str], ...], text: str) -> str | None:
    for keywords, value in keyword_groups:
        if any(keyword in text for keyword in keywords):
            return value
    return None


async def draft(session: AsyncSession, intent: str, *, actor: str) -> Proposal:
    """把一句自然语言变成一条可校验、可展示、可审批的提议。

    `actor` 决定这条提议会被策略引擎判成哪一档 —— 同一个意图,
    不同的执行体拿到的执行档可能完全不同。
    """
    tools = load_tools()
    action = _match(INTENT_KEYWORDS, intent)
    if action is None:
        raise RuleViolation("没能识别出可执行的操作,目前支持:退款申请、物流查询、订单查询")

    order: Order = await resolve_order(session, intent)
    context = await build_order_context(session, order)

    arguments: dict = {"order_id": order.id}
    evidence = [f"订单号 {order.order_no}", f"订单金额 {order.total_amount_cents} 分"]
    rationale_parts = [f"针对订单 {order.order_no}(当前状态 {order.status.value})"]

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
        evidence.append(f"当前可退额度 {context['available_refund_cents']} 分")

    proposal = Proposal(
        action=action,
        arguments=arguments,
        rationale=";".join(rationale_parts),
        evidence=evidence,
        confidence=0.9,
        planner="deterministic",
    )
    # 风险等级与「要不要人批」都来自策略引擎 —— 规划器只负责转述。
    return finalize(proposal, actor=actor, registry=tools)
