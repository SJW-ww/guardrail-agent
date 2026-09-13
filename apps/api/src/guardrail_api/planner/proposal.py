"""提议:模型/规则规划器**唯一**被允许的输出形态。

不是 SQL,不是自由文本,也不是"一段可以解释成任何东西的 JSON"。
结构化提议带来两个好处:

1. 可以被工具 Schema 逐字段校验 —— 模型编的字段进不了执行器;
2. 可以被审批人读懂 —— rationale 和 evidence 是给人看的,不是给日志看的。

注意 `risk_level` / `requires_approval` 的归谁填:
**系统填,模型填了也不算数。** 它们是工具声明与策略引擎的产物,
不是模型的自我评价 —— 让模型给自己的操作定风险等级,等于让它自己发通行证。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class Proposal(BaseModel):
    """一条可校验、可展示、可审批的提议。"""

    action: str = Field(description="要调用的工具名,必须在工具清单内")
    arguments: dict[str, Any] = Field(description="工具入参,必须满足该工具的 JSON Schema")
    rationale: str = Field(description="为什么这么做,给审批人看")
    evidence: list[str] = Field(default_factory=list, description="依据的事实")
    confidence: float = Field(ge=0, le=1, description="规划器对这条提议的把握程度")

    # 下面三个字段由系统覆盖写入(见 planner/deterministic.py 与 planner/llm.py),
    # 模型即使输出也会被丢弃 —— 保留了字段是为了让契约与审计能直接读到。
    risk_level: str = Field(default="", description="工具声明的风险等级,由系统填写")
    requires_approval: bool = Field(
        default=False,
        description="规划器是否**额外**要求人工审批;最终要不要人批,由策略引擎在执行时裁决",
    )
    planner: Literal["deterministic", "llm"] = Field(
        default="deterministic", description="这条提议出自哪条规划链路"
    )
    # 策略引擎的裁决结果转述到这里,给提议卡片与审批人看。
    # 执行时还会再裁决一次,那一次才是真正说了算的。
    policy_decision: str = Field(default="", description="策略引擎裁决:ALLOW / REQUIRE_APPROVAL")
    policy_reason: str = Field(default="", description="裁决理由,给审批人看")
