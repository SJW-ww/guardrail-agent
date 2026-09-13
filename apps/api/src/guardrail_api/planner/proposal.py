"""计划与步骤:规划器**唯一**被允许的输出形态。

不是 SQL,不是自由文本,也不是"一段可以解释成任何东西的 JSON"。
结构化输出带来两个好处:

1. 每一步都能被工具 Schema 逐字段校验 —— 模型编的字段进不了执行器;
2. 每一步都能被审批人读懂 —— rationale 和 evidence 是给人看的,不是给日志看的。

**一份计划可以有一步,也可以有多步。** 多步之间用 `depends_on` 声明先后、
用 `{"$ref": "1.total_amount_cents"}` 传数据(见 `governance.plan`)。
一步的计划仍然是计划 —— 不做"单步"和"多步"两套结构,
否则所有下游代码都要写两遍,而它们本来就该是同一件事。

注意 `risk_level` / `requires_approval` 归谁填:
**系统填,模型填了也不算数。** 它们是工具声明与策略引擎的产物,
不是模型的自我评价 —— 让模型给自己的操作定风险等级,等于让它自己发通行证。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """计划里的一步。校验规则见 `governance.plan.validate`。"""

    seq: int = Field(ge=1, description="步骤号,从 1 开始;执行顺序就是 seq 顺序")
    action: str = Field(description="要调用的工具名,必须在工具清单内")
    arguments: dict[str, Any] = Field(
        description=(
            "工具入参,必须满足该工具的 JSON Schema;"
            '可以用 {"$ref": "1.ticket_id"} 引用更早步骤的产出'
        )
    )
    depends_on: list[int] = Field(
        default_factory=list, description="必须等哪些步骤成功;只能指向更早的步骤"
    )
    rationale: str = Field(description="为什么这么做,给审批人看")
    evidence: list[str] = Field(default_factory=list, description="依据的事实")
    confidence: float = Field(default=0.8, ge=0, le=1, description="规划器对这一步的把握程度")

    # 下面这些由系统覆盖写入(见 planner/finalize.py),模型即使输出也会被丢弃 ——
    # 保留字段是为了让契约与审计能直接读到。
    risk_level: str = Field(default="", description="工具声明的风险等级,由系统填写")
    requires_approval: bool = Field(
        default=False,
        description="规划器是否**额外**要求人工审批;最终要不要人批,由策略引擎在执行时裁决",
    )
    # 策略引擎的裁决结果转述到这里,给提议卡片与审批人看。
    # 执行时还会再裁决一次,那一次才是真正说了算的。
    policy_decision: str = Field(default="", description="策略引擎裁决:ALLOW / REQUIRE_APPROVAL")
    policy_reason: str = Field(default="", description="裁决理由,给审批人看")


class Plan(BaseModel):
    """一次执行的完整计划。落库之后就是 `agent_run.plan`,执行器按 seq 顺序推进。"""

    goal: str = Field(description="这次执行要达成什么,用人话说")
    steps: list[PlanStep] = Field(min_length=1, description="至少一步")
    rationale: str = Field(default="", description="为什么这么编排,给审批人看")
    evidence: list[str] = Field(default_factory=list, description="依据的事实")
    confidence: float = Field(default=0.8, ge=0, le=1)
    planner: Literal["deterministic", "llm", "manual"] = Field(
        default="deterministic", description="这份计划出自哪条链路"
    )

    @property
    def requires_approval(self) -> bool:
        """计划里有没有步骤要人批 —— 提议卡片上的角标读它,决定权仍在策略引擎。"""
        return any(step.requires_approval for step in self.steps)
