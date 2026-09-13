"""策略引擎的规则表:工具风险 x 执行体信任等级。

纯函数,不碰数据库 —— 裁决逻辑必须能脱离执行链路单独验证,
否则「为什么这一步被放行」就永远只能靠一次真实执行来事后推断。
"""

import pytest

from guardrail_api.config import Settings
from guardrail_api.domain.trust import TrustLevel
from guardrail_api.governance.policy import (
    PolicyDecision,
    approval_requirement,
    evaluate,
    evaluate_approval,
    parse_actor_levels,
    resolve_trust_level,
)
from guardrail_api.tools import load_tools

REFUND_ARGS = {"order_id": 1, "reason_code": "QUALITY_ISSUE", "amount_cents": 8000}


def _verdict(tool: str, arguments: dict, level: str, **overrides: object):
    settings = Settings(
        policy_actor_trust_levels=f"agent:bot={level}",
        **overrides,  # type: ignore[arg-type]
    )
    return evaluate(load_tools().get(tool), arguments, actor="agent:bot", settings=settings)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("L0", PolicyDecision.DENY),
        ("L1", PolicyDecision.REQUIRE_APPROVAL),
        ("L2", PolicyDecision.REQUIRE_APPROVAL),
        ("L3", PolicyDecision.REQUIRE_APPROVAL),
        ("L4", PolicyDecision.ALLOW),
    ],
)
def test_high_risk_tool_ladder(level: str, expected: PolicyDecision) -> None:
    """高风险工具:只有 L4 在「幂等 + 可补偿 + 额度内」时才自动执行。"""
    assert _verdict("create_refund", REFUND_ARGS, level).decision is expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("L0", PolicyDecision.DENY),
        ("L1", PolicyDecision.REQUIRE_APPROVAL),
        # close_ticket 是低风险但没有声明补偿动作 —— L2 说好只自动执行「可逆」操作
        ("L2", PolicyDecision.REQUIRE_APPROVAL),
        ("L3", PolicyDecision.ALLOW),
        ("L4", PolicyDecision.ALLOW),
    ],
)
def test_low_risk_tool_ladder(level: str, expected: PolicyDecision) -> None:
    args = {"ticket_id": 1, "reason": "重复工单"}
    assert _verdict("close_ticket", args, level).decision is expected


def test_read_only_tool_is_allowed_at_every_level() -> None:
    for level in TrustLevel:
        verdict = _verdict("query_order", {"order_id": 1}, level.value)
        assert verdict.decision is PolicyDecision.ALLOW, level
        assert verdict.rule == "read_only"


def test_l4_over_quota_escalates_to_approval() -> None:
    verdict = _verdict("create_refund", REFUND_ARGS, "L4", policy_l4_high_risk_limit_cents=5000)

    assert verdict.decision is PolicyDecision.REQUIRE_APPROVAL
    assert verdict.rule == "high_risk_over_limit"
    assert "超过" in verdict.reason


def test_l4_without_amount_escalates_to_approval() -> None:
    args = {"order_id": 1, "reason_code": "QUALITY_ISSUE"}
    verdict = _verdict("create_refund", args, "L4")

    assert verdict.decision is PolicyDecision.REQUIRE_APPROVAL
    assert verdict.rule == "high_risk_without_amount"


def test_reason_always_names_the_actor_and_the_level() -> None:
    verdict = _verdict("create_refund", REFUND_ARGS, "L4")

    assert "agent:bot" in verdict.reason
    assert "L4" in verdict.reason
    assert verdict.trust_level is TrustLevel.L4


def test_unlisted_actor_falls_back_to_default_level() -> None:
    settings = Settings(policy_default_trust_level=TrustLevel.L1)
    level, source = resolve_trust_level("operator-01", settings)

    assert level is TrustLevel.L1
    assert "默认" in source


def test_actor_overrides_are_parsed_and_validated() -> None:
    levels = parse_actor_levels("agent:refund-bot=L4, operator-01=L1")

    assert levels == {"agent:refund-bot": TrustLevel.L4, "operator-01": TrustLevel.L1}

    with pytest.raises(ValueError):
        parse_actor_levels("agent:refund-bot")
    with pytest.raises(ValueError):
        parse_actor_levels("agent:refund-bot=L9")


# ---------- 审批:谁能批、谁不能批 ----------


def test_run_actor_cannot_approve_its_own_operation() -> None:
    """自己批自己不是审批,是把闸门拆了。"""
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="human:operator-01",
        tool="create_refund",
        settings=Settings(),
    )

    assert verdict.decision is PolicyDecision.DENY
    assert verdict.rule == "approval_self"
    assert "职责分离" in verdict.reason


def test_machine_cannot_approve_by_default() -> None:
    """机器人替人签字,等于没有人负责。"""
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="agent:refund-bot",
        tool="create_refund",
        settings=Settings(),
    )

    assert verdict.decision is PolicyDecision.DENY
    assert verdict.rule == "approval_by_machine"


def test_machine_approval_can_be_opened_explicitly() -> None:
    """真要自动化审批,必须显式打开开关 —— 默认不做,做了要认。"""
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="agent:auto-approver",
        tool="create_refund",
        settings=Settings(policy_allow_agent_approval=True),
    )

    assert verdict.decision is PolicyDecision.ALLOW
    assert verdict.rule == "approval_allowed"


def test_a_different_human_can_approve() -> None:
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="supervisor-01",
        tool="create_refund",
        settings=Settings(),
    )

    assert verdict.decision is PolicyDecision.ALLOW
    assert "supervisor-01" in verdict.reason


# ---------- 双人复核:几个签字才算数 ----------


def _requirement(arguments: dict, **overrides: object):
    return approval_requirement(
        load_tools().get("create_refund"),
        arguments,
        settings=Settings(**overrides),  # type: ignore[arg-type]
    )


def test_small_amount_needs_one_signature() -> None:
    assert _requirement(REFUND_ARGS).required == 1


def test_large_amount_needs_two_signatures() -> None:
    requirement = _requirement({"order_id": 1, "amount_cents": 50_000})

    assert requirement.required == 2
    assert "双人复核" in requirement.reason


def test_unknown_amount_is_treated_as_large() -> None:
    """金额算不出来就按最严的处理 —— 不知道要动多少钱,就别让一个人拍板。"""
    requirement = _requirement({"order_id": 1})

    assert requirement.required == 2
    assert "金额未知" in requirement.reason


def test_resolved_amount_can_fill_in_for_a_missing_argument() -> None:
    """金额不在参数里(不传 = 全额退款)时,用工具算出来的实际金额判档,而不是当未知。"""
    spec = load_tools().get("create_refund")
    args = {"order_id": 1}  # 没传 amount_cents

    assert (
        approval_requirement(spec, args, settings=Settings(), resolved_amount=2_000).required == 1
    )
    assert (
        approval_requirement(spec, args, settings=Settings(), resolved_amount=50_000).required == 2
    )


def test_duplicate_signature_does_not_count_twice() -> None:
    """同一个人的第二次请求是幂等的,但绝不能把 1/2 变成 2/2。"""
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="supervisor-01",
        tool="create_refund",
        existing_approvers=["supervisor-01"],
        required=2,
        settings=Settings(),
    )

    assert verdict.decision is PolicyDecision.ALLOW
    assert verdict.rule == "approval_duplicate"
    assert "1/2" in verdict.reason


def test_same_role_cannot_fill_the_second_signature() -> None:
    """两个主管互相签字不算复核 —— 双人复核要的是不同角色,不是不同工号。"""
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="supervisor-02",
        tool="create_refund",
        existing_approvers=["supervisor-01"],
        required=2,
        settings=Settings(
            policy_approver_roles="supervisor-01=supervisor,supervisor-02=supervisor"
        ),
    )

    assert verdict.decision is PolicyDecision.DENY
    assert verdict.rule == "approval_same_role"


def test_a_different_role_completes_the_second_signature() -> None:
    verdict = evaluate_approval(
        run_actor="human:operator-01",
        approver="finance-01",
        tool="create_refund",
        existing_approvers=["supervisor-01"],
        required=2,
        settings=Settings(policy_approver_roles="supervisor-01=supervisor,finance-01=finance"),
    )

    assert verdict.decision is PolicyDecision.ALLOW
    assert verdict.rule == "approval_allowed"
    assert "2/2" in verdict.reason
