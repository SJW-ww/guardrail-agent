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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from guardrail_api.config import Settings
from guardrail_api.domain.trust import TrustLevel
from guardrail_api.tools.base import RiskLevel, ToolSpec


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


def _amount_of(spec: ToolSpec, arguments: Mapping[str, Any]) -> int | None:
    """金额只能从工具声明过的字段里读。没声明就当作"无法套用额度"处理。"""
    if spec.amount_field is None:
        return None
    value = arguments.get(spec.amount_field)
    if value is None:
        return None
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
        return _evaluate_high_risk(spec, arguments, trust=trust, source=source, settings=settings)

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


def _evaluate_high_risk(
    spec: ToolSpec,
    arguments: Mapping[str, Any],
    *,
    trust: TrustLevel,
    source: str,
    settings: Settings,
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

    amount = _amount_of(spec, arguments)
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
