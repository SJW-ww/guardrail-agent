"""确定性规划器:规则解析意图 → 结构化计划。不调用任何模型。

它的存在有两个理由,不是一个"降级方案":

- **离线可跑。** CI 与本地演示不依赖任何外部服务,地基不稳时先别急着接模型。
- **对照物。** 同一个意图同时喂给规则和模型,能直接看出"模型的贡献到底在哪"。

规则规划器也会输出**多步计划**:"按可退额度全额退款"这种意图,
正确做法是先查额度、再拿着查到的数字去申请退款,而不是让退款工具
在最后一刻自己去算 —— 金额写进计划里,审批人和审计才看得到它。
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.domain.errors import RuleViolation
from guardrail_api.models import Order
from guardrail_api.planner.context import build_order_context, resolve_order
from guardrail_api.planner.finalize import finalize
from guardrail_api.planner.proposal import Plan, PlanStep
from guardrail_api.tools import load_tools

NUMBER_PATTERN = re.compile(r"(\d+)\s*(?:元|块|块钱)")
#: 「全退 / 全额 / 全款」= 按可退额度退,金额要先查出来
FULL_REFUND_KEYWORDS = ("全额", "全款", "全退", "都退")

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


def _refund_step(intent: str, order: Order, context: dict, *, full_refund: bool) -> PlanStep:
    reason_code = _match(REASON_KEYWORDS, intent)
    if reason_code is None:
        raise RuleViolation("申请退款需要说明原因,例如:质量问题 / 发错货 / 运输破损 / 到货太慢")

    arguments: dict = {"order_id": order.id, "reason_code": reason_code}
    rationale = f"为订单 {order.order_no} 申请退款,原因码 {reason_code}"

    amount_match = NUMBER_PATTERN.search(intent)
    if amount_match:
        amount_cents = int(amount_match.group(1)) * 100
        arguments["amount_cents"] = amount_cents
        rationale += f",金额 {amount_cents} 分"
    elif full_refund:
        # 金额来自第 1 步查到的可退额度。**这是这一步存在的全部意义**:
        # 金额是策略引擎的输入(能不能自动执行、要不要双人复核都看它),
        # 让它以占位符形态进入计划,等于让审批人对着一个问号签字。
        arguments["amount_cents"] = {"$ref": "1.available_refund_cents"}
        rationale += ",按第 1 步查到的可退额度全额申请"
    else:
        rationale += ",金额未指定,按工具默认(全额)申请"

    return PlanStep(
        seq=2 if full_refund else 1,
        action="create_refund",
        arguments=arguments,
        depends_on=[1] if full_refund else [],
        rationale=rationale,
        evidence=[f"当前可退额度 {context['available_refund_cents']} 分"],
        confidence=0.9,
    )


async def draft(session: AsyncSession, intent: str, *, actor: str) -> Plan:
    """把一句自然语言变成一份可校验、可展示、可审批的计划。

    `actor` 决定这份计划会被策略引擎判成哪一档 —— 同一个意图,
    不同的执行体拿到的执行档可能完全不同。
    """
    tools = load_tools()
    action = _match(INTENT_KEYWORDS, intent)
    if action is None:
        raise RuleViolation("没能识别出可执行的操作,目前支持:退款申请、物流查询、订单查询")

    order: Order = await resolve_order(session, intent)
    context = await build_order_context(session, order)
    evidence = [f"订单号 {order.order_no}", f"订单金额 {order.total_amount_cents} 分"]
    full_refund = any(keyword in intent for keyword in FULL_REFUND_KEYWORDS)

    steps: list[PlanStep]
    if action == "create_refund" and full_refund:
        # 两步:先查额度,再按查到的额度退款。金额因此是**明确写进计划**的,
        # 而不是留给工具在最后一步自己算 —— 审批人签的就是这个数字。
        steps = [
            PlanStep(
                seq=1,
                action="query_refundable",
                arguments={"order_id": order.id},
                rationale=f"先读出订单 {order.order_no} 的可退额度,作为下一步的退款金额",
                evidence=list(evidence),
                confidence=0.95,
            ),
            _refund_step(intent, order, context, full_refund=True),
        ]
    elif action == "create_refund":
        steps = [_refund_step(intent, order, context, full_refund=False)]
    else:
        read_args = (
            {"order_id": order.id} if action == "query_order" else {"order_no": order.order_no}
        )
        what = "物流" if action == "query_logistics" else "详情"
        steps = [
            PlanStep(
                seq=1,
                action=action,
                arguments=read_args,
                rationale=f"查询订单 {order.order_no} 的{what}",
                evidence=list(evidence),
                confidence=0.9,
            )
        ]

    plan = Plan(
        goal=f"针对订单 {order.order_no}:{intent}",
        steps=steps,
        rationale=";".join(step.rationale for step in steps),
        evidence=evidence,
        confidence=min(step.confidence for step in steps),
        planner="deterministic",
    )
    # 风险等级与「要不要人批」都来自策略引擎 —— 规划器只负责转述。
    return finalize(plan, actor=actor, registry=tools)
