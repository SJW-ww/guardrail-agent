"""把计划交给策略引擎定档。

规划器只负责「提议什么」,不负责「要不要人批」。但提议卡片要给人看,
得让提出的人和审批人一眼知道这份计划会走到哪一步 —— 所以这里**去问策略引擎**,
把它的裁决原样转述到每一步上,而不是自己拍一个布尔值。

一次提议会被裁决两次(这里一次、执行时一次),这不是重复:

- 这里是为了**告诉人**这份计划会怎么走;
- 执行时那次才是真正说了算的,而且用的是当时的权限 —— 权限可能在这中间被收回。

**计划里任何一步被拒绝,整份计划就该被拒绝。** 让用户看到一个"前两步能做、
第三步做不了"的方案,等于让他先做完两步再撞墙 —— 拒绝要发生在提议这一步。
"""

from guardrail_api.config import Settings, get_settings
from guardrail_api.domain.errors import PolicyDenied
from guardrail_api.governance.policy import PolicyDecision, evaluate
from guardrail_api.planner.proposal import Plan, PlanStep
from guardrail_api.tools.registry import ToolRegistry, load_tools


def finalize_step(
    step: PlanStep,
    *,
    actor: str,
    registry: ToolRegistry,
    settings: Settings,
) -> PlanStep:
    """按工具声明与执行体信任等级,给单步补上风险等级、审批要求与裁决理由。"""
    spec = registry.get(step.action)
    verdict = evaluate(spec, step.arguments, actor=actor, settings=settings)

    if verdict.decision is PolicyDecision.DENY:
        # 连提议都不该生成:让模型先"想一个方案",再由系统拒绝,
        # 只会让用户以为这件事有戏。
        raise PolicyDenied(
            f"执行体 {actor} 无权执行第 {step.seq} 步 {spec.name}:{verdict.reason}",
            actor=actor,
            tool=spec.name,
            step_seq=step.seq,
            rule=verdict.rule,
        )

    step.risk_level = spec.risk_level.value
    step.requires_approval = verdict.decision is PolicyDecision.REQUIRE_APPROVAL
    step.policy_decision = verdict.decision.value
    step.policy_reason = verdict.reason
    return step


def finalize(
    plan: Plan,
    *,
    actor: str,
    registry: ToolRegistry | None = None,
    settings: Settings | None = None,
) -> Plan:
    """整份计划:每一步都定档;任何一步被拒绝,整份计划被拒绝。"""
    tools = registry or load_tools()
    resolved = settings or get_settings()
    plan.steps = [
        finalize_step(step, actor=actor, registry=tools, settings=resolved) for step in plan.steps
    ]
    return plan
