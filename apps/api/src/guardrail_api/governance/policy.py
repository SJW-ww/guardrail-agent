"""策略引擎:模型只提议,**系统做决策**。

三种裁决,每种都必须带上能给人看的理由(这是项目的硬性原则):

- `ALLOW`            直接执行;
- `REQUIRE_APPROVAL` 挂起,等人点一下;
- `DENY`             拒绝执行。

规则表(工具风险 x 执行体信任等级):

|      | 只读工具 | 低风险写          | 高风险写                          |
|------|----------|-------------------|-----------------------------------|
| L0   | ALLOW    | DENY              | DENY                              |
| L1   | ALLOW    | REQUIRE_APPROVAL  | REQUIRE_APPROVAL                  |
| L2   | ALLOW    | ALLOW(需声明补偿) | REQUIRE_APPROVAL                  |
| L3   | ALLOW    | ALLOW             | REQUIRE_APPROVAL                  |
| L4   | ALLOW    | ALLOW             | ALLOW(可补偿+幂等+额度内),否则审批 |

两个刻意的取舍:

1. **高风险不是一刀切,但默认一刀切。** L0-L3 下高风险工具一律人工审批。
   只有 L4 在**同时**满足「声明了补偿动作」「幂等」「金额未超单笔额度」时才自动执行 ——
   放行凭据是平台配置的额度,不是模型给自己开的绿灯。少任何一个条件就自动升级。
2. **L2 要求低风险工具声明补偿动作。** 契约里 L2 的原话是"低风险**可逆**操作自动执行",
   可逆性必须有声明才有依据(`compensate_tool`)。没声明补偿动作的低风险工具,
   在 L2 下也走审批;到 L3 才自动执行。

裁决理由是给人看的:审批人要知道自己批的是什么,审计要能解释当时为什么放行。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from guardrail_api.config import Settings
from guardrail_api.domain.trust import TrustLevel
from guardrail_api.tools.base import RiskLevel, ToolSpec


@dataclass(frozen=True, slots=True)
class ApprovalRequirement:
    """一次审批要集齐几个人的签字,以及为什么。"""

    required: int
    reason: str


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    decision: PolicyDecision
    rule: str
    reason: str
    trust_level: TrustLevel

    @property
    def allows_execution(self) -> bool:
        return self.decision is PolicyDecision.ALLOW

    def to_dict(self) -> dict[str, str]:
        """落进 run.checkpoint,给前端直接展示,不用前端再翻译一次。"""
        return {
            "decision": self.decision.value,
            "rule": self.rule,
            "reason": self.reason,
            "trust_level": self.trust_level.value,
        }


def parse_actor_levels(raw: str) -> dict[str, TrustLevel]:
    """解析 `agent:refund-bot=L4,operator-01=L2` 形式的按执行体授权配置。

    写得不对就明确报错,不静默忽略 —— 一条没生效的授权看起来和"已生效"一模一样,
    这类配置错误只能靠报错暴露。
    """
    levels: dict[str, TrustLevel] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        actor, _, level = item.partition("=")
        actor, level = actor.strip(), level.strip()
        if not actor or not level:
            raise ValueError(f"信任等级配置格式应为 actor=L4,收到:{item!r}")
        try:
            levels[actor] = TrustLevel(level)
        except ValueError as exc:
            options = [item.value for item in TrustLevel]
            raise ValueError(f"未知的信任等级 {level!r}(可选:{options})") from exc
    return levels


def resolve_trust_level(actor: str, settings: Settings) -> tuple[TrustLevel, str]:
    """信任等级来自平台配置,不来自模型,也不来自调用方传参。"""
    overrides = parse_actor_levels(settings.policy_actor_trust_levels)
    if actor in overrides:
        level = overrides[actor]
        return level, f"执行体 {actor} 在策略配置里被授予 {level.value}"
    level = settings.policy_default_trust_level
    return level, f"执行体 {actor} 未单独配置,采用默认信任等级 {level.value}"


def _amount_of(
    spec: ToolSpec, arguments: Mapping[str, Any], *, resolved: int | None = None
) -> int | None:
    """金额只能从工具声明过的字段里读。没声明就当作"无法套用额度"处理。

    `resolved` 是调用方(执行器/网关)用工具的 `amount_resolver` 算出来的实际金额,
    用于「金额不在参数里」的情况:例如 create_refund 不传金额 = 全额退款,
    真实金额得从订单上算。策略引擎不查库,它只消费这个结果。
    """
    if spec.amount_field is None:
        return None
    value = arguments.get(spec.amount_field)
    if value is None:
        return resolved
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def evaluate(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    actor: str,
    settings: Settings,
    resolved_amount: int | None = None,
) -> PolicyVerdict:
    """对一次工具调用做出裁决。纯函数 —— 同样的输入永远得到同样的结论,方便测试与复盘。"""
    trust, source = resolve_trust_level(actor, settings)

    def verdict(decision: PolicyDecision, rule: str, reason: str) -> PolicyVerdict:
        return PolicyVerdict(decision=decision, rule=rule, reason=reason, trust_level=trust)

    # 只读工具不产生副作用,任何档位都放行
    if spec.is_read_only:
        return verdict(
            PolicyDecision.ALLOW, "read_only", f"工具 {spec.name} 是只读查询,不产生副作用"
        )

    # L0:只读档,写操作一律拒绝
    if trust is TrustLevel.L0:
        return verdict(
            PolicyDecision.DENY,
            "trust_l0_read_only",
            f"{source},{TrustLevel.L0.value} 档不允许执行写操作 {spec.name}",
        )

    # L1:建议模式,写操作全部人工确认
    if trust is TrustLevel.L1:
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "trust_l1_suggest_only",
            f"{source},L1 建议模式下写操作 {spec.name} 必须人工确认",
        )

    if spec.risk_level is RiskLevel.HIGH:
        return _evaluate_high_risk(
            spec,
            arguments,
            trust=trust,
            source=source,
            settings=settings,
            resolved_amount=resolved_amount,
        )

    # 低风险写:L2 要求可逆(有补偿动作),L3 及以上直接放行
    if trust is TrustLevel.L2 and not spec.compensate_tool:
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "low_risk_not_compensable",
            f"{source},L2 只自动执行可逆操作,而 {spec.name} 未声明补偿动作,故转人工审批",
        )
    return verdict(
        PolicyDecision.ALLOW,
        "low_risk_auto",
        f"{source},低风险写操作 {spec.name} 在 {trust.value} 档可自动执行",
    )


def parse_roles(raw: str) -> dict[str, str]:
    """解析 `审批人=角色` 配置,格式 `supervisor-01=supervisor,finance-01=finance`。"""
    roles: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        actor, _, role = item.partition("=")
        actor, role = actor.strip(), role.strip()
        if not actor or not role:
            raise ValueError(f"角色配置格式应为 actor=role,收到:{item!r}")
        roles[actor] = role
    return roles


def role_of(approver: str, settings: Settings) -> str:
    """审批人的角色。

    **没配角色的人自成一种角色**(用身份当角色)。这个默认让「必须换个人签字」
    不需要任何配置就成立;配了角色之后才知道「主管 + 财务」这种组织约束。
    """
    return parse_roles(settings.policy_approver_roles).get(approver, approver)


def approval_requirement(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    settings: Settings,
    resolved_amount: int | None = None,
) -> ApprovalRequirement:
    """这一步需要几个人签字。金额越大,越不该由一个人说了算。"""
    if spec.risk_level is not RiskLevel.HIGH:
        return ApprovalRequirement(required=1, reason=f"{spec.name} 不是高风险操作,一个人签字即可")

    threshold = settings.policy_dual_approval_threshold_cents
    amount = _amount_of(spec, arguments, resolved=resolved_amount)
    if amount is None:
        # 金额未知时从严:不知道要动多少钱,就别让一个人拍板
        return ApprovalRequirement(
            required=2,
            reason=f"{spec.name} 是高风险操作但本次金额未知,从严要求双人复核",
        )
    if amount > threshold:
        return ApprovalRequirement(
            required=2,
            reason=(f"金额 {amount} 分超过双人复核阈值 {threshold} 分,需要两个不同角色先后签字"),
        )
    return ApprovalRequirement(
        required=1,
        reason=f"金额 {amount} 分未超过双人复核阈值 {threshold} 分,一个人签字即可",
    )


def evaluate_approval(
    *,
    run_actor: str,
    approver: str,
    tool: str,
    settings: Settings,
    existing_approvers: Sequence[str] = (),
    required: int = 1,
) -> PolicyVerdict:
    """审批本身也要裁决:**谁能批、谁不能批、这一票算不算数**,同样给理由。

    四条规则,按顺序:

    1. **不能自己批自己。** 发起这次执行的执行体再点一次「批准」,那不是审批,
       是把闸门拆了。职责分离(separation of duties)是最古老也最有效的一条控制。
    2. **机器不能替人做审批决定。** 审批的意义在于「有个人愿意为这次写操作负责」;
       一个 agent: 前缀的调用者点批准,没人因此负责。
    3. **同一人重复签字不重复计数。** 不是错误 —— 请求重发本来就该是幂等的,
       但它也绝不能把 1/2 变成 2/2。
    4. **双人复核要求不同角色。** 两个主管互相签字不算复核。

    这些规则只约束「谁」,不约束「批的是哪一步」—— 后者是 evaluate() 的事。
    """
    trust, source = resolve_trust_level(approver, settings)

    if approver == run_actor:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            rule="approval_self",
            reason=f"{approver} 是这次执行的发起者,不能批准自己的操作(职责分离)",
            trust_level=trust,
        )

    if approver.startswith("agent:") and not settings.policy_allow_agent_approval:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            rule="approval_by_machine",
            reason=(
                f"{approver} 是机器执行体,默认不能替人做审批决定"
                "(如需自动化审批,请显式打开 POLICY_ALLOW_AGENT_APPROVAL 并自担风险)"
            ),
            trust_level=trust,
        )

    if approver in existing_approvers:
        return PolicyVerdict(
            decision=PolicyDecision.ALLOW,
            rule="approval_duplicate",
            reason=(
                f"{approver} 已经为这一步签过字,不重复计数"
                f"(当前 {len(existing_approvers)}/{required})"
            ),
            trust_level=trust,
        )

    existing_roles = {role_of(item, settings) for item in existing_approvers}
    my_role = role_of(approver, settings)
    if my_role in existing_roles:
        return PolicyVerdict(
            decision=PolicyDecision.DENY,
            rule="approval_same_role",
            reason=(
                f"已由 {sorted(existing_approvers)} 以角色「{my_role}」签字,"
                f"{approver} 的角色相同 —— 双人复核要的是不同角色,不是不同工号"
            ),
            trust_level=trust,
        )

    collected = len(existing_approvers) + 1
    return PolicyVerdict(
        decision=PolicyDecision.ALLOW,
        rule="approval_allowed",
        reason=(f"{approver}(角色 {my_role},{source})签字 {collected}/{required};责任落到该审批人"),
        trust_level=trust,
    )


def _evaluate_high_risk(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    trust: TrustLevel,
    source: str,
    settings: Settings,
    resolved_amount: int | None = None,
) -> PolicyVerdict:
    def verdict(decision: PolicyDecision, rule: str, reason: str) -> PolicyVerdict:
        return PolicyVerdict(decision=decision, rule=rule, reason=reason, trust_level=trust)

    if trust is not TrustLevel.L4:
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "high_risk_requires_approval",
            f"{source},而 {spec.name} 声明为高风险(动钱/动库存),{trust.value} 档必须人工审批",
        )

    # L4 受限自主:四个条件同时成立才自动执行,少一个就升级
    if not (spec.idempotent and spec.compensate_tool):
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "high_risk_not_compensable",
            f"{source},但 {spec.name} 未同时声明「幂等 + 补偿动作」,"
            "自动执行的前提是出错能回退,故转人工审批",
        )

    amount = _amount_of(spec, arguments, resolved=resolved_amount)
    if amount is None:
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "high_risk_without_amount",
            f"{source},但 {spec.name} 未声明金额字段或本次未给出金额,无法套用额度,故转人工审批",
        )

    limit = settings.policy_l4_high_risk_limit_cents
    if amount > limit:
        return verdict(
            PolicyDecision.REQUIRE_APPROVAL,
            "high_risk_over_limit",
            f"{source},但金额 {amount} 分超过 L4 单笔自主额度 {limit} 分,自动升级为人工审批",
        )

    return verdict(
        PolicyDecision.ALLOW,
        "high_risk_within_l4_quota",
        f"{source},且 {spec.name} 幂等、可补偿(补偿动作 {spec.compensate_tool}),"
        f"金额 {amount} 分未超过单笔自主额度 {limit} 分 —— 额度内受限自主执行",
    )
