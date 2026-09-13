"""LLM 规划器的验收:模型只规划,校验与定档全在系统侧。

模型服务用 `httpx.MockTransport` 假装 —— 这里要验证的是**我们怎么对待模型的输出**,
不是模型本身有多聪明。真实模型的波动不该让 CI 变红,所以真正的 LLM 调用不进测试套件。
"""

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.config import Settings
from guardrail_api.domain.errors import PolicyDenied, ProposalRejected
from guardrail_api.models import Customer, Inventory, Order, Product
from guardrail_api.planner.llm import LLMPlanner
from guardrail_api.services import order as order_service
from guardrail_api.services.order import Address, OrderLine
from guardrail_api.tools import load_tools

pytestmark = pytest.mark.integration

ADDRESS = Address(receiver_name="张三", receiver_phone="13800000000", address="深圳市南山区 1 号")


async def _seed_paid_order(session: AsyncSession, *, index: int = 1) -> Order:
    customer = Customer(
        name=f"客户{index:03d}", email=f"customer{index:03d}@example.com", phone="13800000000"
    )
    product = Product(
        sku=f"SKU-{index:04d}", name=f"商品{index:04d}", category="demo", price_cents=1000
    )
    session.add_all([customer, product])
    await session.flush()
    session.add(Inventory(product_id=product.id, available_qty=10, reserved_qty=0))
    await session.flush()

    order = await order_service.create_order(
        session,
        customer_id=customer.id,
        lines=[OrderLine(product_id=product.id, quantity=2)],
        address=ADDRESS,
        order_no=f"SO2026{index:06d}",
    )
    await order_service.pay_order(session, order.id)
    await session.commit()
    return order


class _FakeModel:
    """按顺序吐出预设回复,并记录收到的请求,供断言"回喂重试"是否真的发生。"""

    def __init__(self, *replies: dict[str, Any] | str) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        reply = self._replies.pop(0)
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "model": "fake-model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "choices": [{"message": {"content": content}}],
            },
        )


def _planner(model: _FakeModel, **overrides: Any) -> LLMPlanner:
    settings = Settings(
        planner_backend="llm",
        llm_api_key="sk-test",
        llm_base_url="https://llm.test/v1",
        **overrides,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(model))
    return LLMPlanner(settings=settings, registry=load_tools(), client=client)


def _plan(step: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """把一步的回复包成计划。模型输出的是计划,不是孤零零的一步。"""
    return {
        "goal": extra.pop("goal", "处理这张订单"),
        "steps": [{"seq": 1, "depends_on": [], "evidence": [], "confidence": 0.8, **step}],
        **extra,
    }


async def test_valid_plan_is_passed_through_with_system_decided_risk(
    session: AsyncSession,
) -> None:
    order = await _seed_paid_order(session)
    model = _FakeModel(
        _plan(
            {
                "action": "create_refund",
                "arguments": {
                    "order_id": order.id,
                    "reason_code": "QUALITY_ISSUE",
                    "amount_cents": 8000,
                },
                "rationale": "客户反馈质量问题,申请退 80 元",
                "evidence": ["订单 SO2026000001 状态 PAID"],
                # 模型试图给自己降档 —— 必须被系统覆盖,不能让模型给自己发通行证
                "confidence": 0.8,
                "risk_level": "read_only",
                "requires_approval": False,
            }
        )
    )

    intent = f"订单 {order.order_no} 质量有问题,帮我退 80 元"

    plan = await _planner(model).draft(session, intent, actor="agent:guardrail")

    assert [step.action for step in plan.steps] == ["create_refund"]
    step = plan.steps[0]
    assert step.arguments["amount_cents"] == 8000
    assert step.risk_level == "high"
    # 审批要求由策略引擎给出:默认 L2 档的执行体,高风险操作必须人批
    assert step.requires_approval is True
    assert step.policy_decision == "REQUIRE_APPROVAL"
    assert "L2" in step.policy_reason
    assert plan.planner == "llm"
    assert plan.goal
    assert len(model.requests) == 1


async def test_multi_step_plan_with_a_reference_is_accepted(session: AsyncSession) -> None:
    """多步 + 引用要能通过校验:引用是参数的一种合法取值,不是"字段类型不对"。"""
    order = await _seed_paid_order(session)
    model = _FakeModel(
        {
            "goal": "按可退额度全额退款",
            "rationale": "先查额度,再按查到的额度申请退款",
            "confidence": 0.85,
            "steps": [
                {
                    "seq": 1,
                    "action": "query_order",
                    "arguments": {"order_id": order.id},
                    "rationale": "先读出可退额度",
                    "confidence": 0.95,
                },
                {
                    "seq": 2,
                    "action": "create_refund",
                    "arguments": {
                        "order_id": order.id,
                        "reason_code": "QUALITY_ISSUE",
                        "amount_cents": {"$ref": "1.total_amount_cents"},
                    },
                    "depends_on": [1],
                    "rationale": "按第 1 步查到的金额全额退",
                    "confidence": 0.9,
                },
            ],
        }
    )

    plan = await _planner(model).draft(
        session, f"订单 {order.order_no} 质量有问题,退全款", actor="agent:guardrail"
    )

    assert [step.action for step in plan.steps] == ["query_order", "create_refund"]
    assert plan.steps[1].depends_on == [1]
    assert plan.steps[1].arguments["amount_cents"] == {"$ref": "1.total_amount_cents"}
    assert len(model.requests) == 1


async def test_dependency_on_a_later_step_is_rejected_then_repaired(
    session: AsyncSession,
) -> None:
    """依赖只能往回指。模型写了依赖后面的步骤,直接把错误回喂给它重写。"""
    order = await _seed_paid_order(session)
    bad = {
        "goal": "颠倒的计划",
        "steps": [
            {
                "seq": 1,
                "action": "query_order",
                "arguments": {"order_id": order.id},
                "depends_on": [2],
                "rationale": "先依赖第 2 步",
                "confidence": 0.9,
            },
            {
                "seq": 2,
                "action": "query_order",
                "arguments": {"order_id": order.id},
                "rationale": "后做的第 2 步",
                "confidence": 0.9,
            },
        ],
    }
    model = _FakeModel(
        bad,
        _plan(
            {
                "action": "query_order",
                "arguments": {"order_id": order.id},
                "rationale": "改成一步就够",
            }
        ),
    )

    plan = await _planner(model).draft(
        session, f"帮我查一下订单 {order.order_no}", actor="agent:guardrail"
    )

    assert [step.action for step in plan.steps] == ["query_order"]
    assert len(model.requests) == 2
    repair_message = model.requests[1]["messages"][-1]["content"]
    assert "更早" in repair_message


async def test_hallucinated_order_id_is_rejected_then_repaired(session: AsyncSession) -> None:
    order = await _seed_paid_order(session)
    model = _FakeModel(
        _plan(
            {
                "action": "create_refund",
                "arguments": {"order_id": 999999, "reason_code": "QUALITY_ISSUE"},
                "rationale": "退到另一个订单上",
                "confidence": 0.9,
            }
        ),
        _plan(
            {
                "action": "create_refund",
                "arguments": {"order_id": order.id, "reason_code": "QUALITY_ISSUE"},
                "rationale": "改为已解析出的订单",
                "confidence": 0.9,
            }
        ),
    )

    plan = await _planner(model).draft(
        session, f"订单 {order.order_no} 质量有问题,帮我退款", actor="agent:guardrail"
    )

    assert plan.steps[0].arguments["order_id"] == order.id
    assert len(model.requests) == 2
    # 第二次请求必须带上第一次的错误原因,否则重试只是重新掷骰子
    repair_message = model.requests[1]["messages"][-1]["content"]
    assert "order_id=999999" in repair_message


async def test_unknown_tool_exhausts_repair_attempts(session: AsyncSession) -> None:
    order = await _seed_paid_order(session)
    bad = _plan(
        {
            "action": "delete_order",
            "arguments": {"order_id": order.id},
            "rationale": "编一个不存在的工具",
            "confidence": 0.5,
        }
    )
    model = _FakeModel(bad, bad, bad)

    with pytest.raises(ProposalRejected) as excinfo:
        await _planner(model, llm_max_repair_attempts=2).draft(
            session, f"订单 {order.order_no} 我不想要了", actor="agent:guardrail"
        )

    assert len(model.requests) == 3
    assert len(excinfo.value.context["attempts"]) == 3


async def test_fenced_json_block_is_accepted(session: AsyncSession) -> None:
    order = await _seed_paid_order(session)
    fenced = (
        "```json\n"
        + json.dumps(
            _plan(
                {
                    "action": "query_order",
                    "arguments": {"order_id": order.id},
                    "rationale": "客户想查订单",
                    "evidence": ["订单号已解析"],
                    "confidence": 0.95,
                }
            ),
            ensure_ascii=False,
        )
        + "\n```"
    )
    model = _FakeModel(fenced)

    plan = await _planner(model).draft(
        session, f"帮我查一下订单 {order.order_no}", actor="agent:guardrail"
    )

    assert [step.action for step in plan.steps] == ["query_order"]
    assert plan.steps[0].risk_level == "read_only"
    assert plan.steps[0].policy_decision == "ALLOW"


async def test_intent_without_order_reference_is_rejected_before_calling_model(
    session: AsyncSession,
) -> None:
    model = _FakeModel(_plan({"action": "query_order", "arguments": {}, "rationale": ""}))

    with pytest.raises(Exception) as excinfo:
        await _planner(model).draft(session, "帮我查一下订单", actor="agent:guardrail")

    assert getattr(excinfo.value, "code", None) == "rule_violation"
    assert model.requests == []


async def test_read_only_executor_is_denied_at_proposal_time(session: AsyncSession) -> None:
    """只读档的执行体连提议都不该拿到 —— 让模型先想方案再由系统拒绝,是骗人。"""
    order = await _seed_paid_order(session)
    model = _FakeModel(
        _plan(
            {
                "action": "create_refund",
                "arguments": {"order_id": order.id, "reason_code": "QUALITY_ISSUE"},
                "rationale": "试一下能不能退",
                "evidence": [],
                "confidence": 0.9,
            }
        )
    )
    planner = _planner(model, policy_actor_trust_levels="agent:guardrail=L0")

    with pytest.raises(PolicyDenied) as excinfo:
        await planner.draft(
            session, f"订单 {order.order_no} 质量有问题,帮我退款", actor="agent:guardrail"
        )

    assert excinfo.value.context["rule"] == "trust_l0_read_only"
    assert "L0" in excinfo.value.message
