"""把提议交给策略引擎定档。

规划器只负责「提议什么」,不负责「要不要人批」。但提议卡片要给人看,
得让提出的人和审批人一眼知道这条提议会走到哪一步 —— 所以这里**去问策略引擎**,
把它的裁决原样转述到提议上,而不是自己拍一个布尔值。

一次提议会被裁决两次(这里一次、执行时一次),这不是重复:
- 这里是为了**告诉人**这条提议会怎么走;
- 执行时那次才是真正说了算的,而且用的是当时的权限 —— 权限可能在这中间被收回。
"""

from guardrail_api.config import Settings, get_settings
from guardrail_api.domain.errors import PolicyDenied
from guardrail_api.governance.policy import PolicyDecision, evaluate
from guardrail_api.planner.proposal import Proposal
from guardrail_api.tools.registry import ToolRegistry, load_tools


def finalize(
    proposal: Proposal,
    *,
    actor: str,
    registry: ToolRegistry | None = None,
    settings: Settings | None = None,
) -> Proposal:
    """按工具声明与执行体信任等级补齐风险等级、审批要求与裁决理由。"""
    tools = registry or load_tools()
    spec = tools.get(proposal.action)
    verdict = evaluate(spec, proposal.arguments, actor=actor, settings=settings or get_settings())

    if verdict.decision is PolicyDecision.DENY:
        # 连提议都不该生成:让模型先"想一个方案",再由系统拒绝,
        # 只会让用户以为这件事有戏。拒绝要发生在提议这一步。
        raise PolicyDenied(
            f"执行体 {actor} 无权执行 {spec.name}:{verdict.reason}",
            actor=actor,
            tool=spec.name,
            rule=verdict.rule,
        )

    proposal.risk_level = spec.risk_level.value
    proposal.requires_approval = verdict.decision is PolicyDecision.REQUIRE_APPROVAL
    proposal.policy_decision = verdict.decision.value
    proposal.policy_reason = verdict.reason
    return proposal
