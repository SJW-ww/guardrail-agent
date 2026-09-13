"""工具契约。

**工具声明即护栏。** 参数 Schema、风险级别、前置条件、副作用、幂等策略、补偿动作,
全部写在 `ToolSpec` 里,而不是散在 handler 内部:

- 编排层(W3 的 Agent)只读这份声明,不需要理解工具实现;
- 策略引擎(W4)按 `risk_level` 决定 ALLOW / REQUIRE_APPROVAL / DENY;
- 执行器(W2)按 `idempotency_key` 落幂等表,按 `compensate_tool` 编排补偿。

新增一个工具只需要新增一个声明,编排层与治理层都不改代码。
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession


class RiskLevel(StrEnum):
    """工具的风险级别 —— 策略引擎的第一输入。"""

    READ_ONLY = "read_only"
    LOW = "low"  # 可逆的低风险写:建工单、改地址
    HIGH = "high"  # 动钱动库存:退款、改价


@dataclass(slots=True)
class ToolContext:
    """一次工具调用的上下文。agent / human 的身份从认证上下文注入,不由模型提供。"""

    session: AsyncSession
    actor: str
    run_id: str | None = None
    tenant_id: str = "default"

    @property
    def is_agent(self) -> bool:
        return self.actor.startswith("agent:")


ToolHandler = Callable[[ToolContext, Any], Awaitable[BaseModel]]

# 审计快照:给定参数,返回这次调用**会动到的那部分世界**的当前值。
# 执行器会在 handler 前后各调一次,差集就是这次写操作的 before / after。
# 返回结构必须可 JSON 序列化,且只包含被本工具影响的实体 ——
# 审计表里不该出现和这次操作无关的字段,否则没人看得懂 diff。
ToolSnapshot = Callable[[ToolContext, Any], Awaitable[dict[str, Any]]]

# 金额解析:有些工具的金额可以不传(例如 create_refund 不传 = 全额退款),
# 此时「这次操作要动多少钱」只有工具自己知道 —— 策略引擎据此决定要不要双人复核。
# 拿不到就返回 None,调用方按「金额未知从严」处理,绝不当成 0。
AmountResolver = Callable[[ToolContext, Any], Awaitable[int | None]]


@dataclass(frozen=True, slots=True)
class CompensateArg:
    """补偿动作的参数从哪来。

    只有两种来源,刻意不支持表达式:

    - `result_path`:从**原步骤的产出**里取(例:退款工单的 `ticket_id`);
    - `const`:写死的字面量(例:"计划未完成,自动撤销")。

    取不到值就是**不能自动补偿** —— 不猜、不用默认值。补偿写错比不补偿更糟:
    它会以"系统自动回滚"的名义动生产数据。
    """

    result_path: str | None = None
    const: Any = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    description: str
    risk_level: RiskLevel
    params_model: type[BaseModel]
    handler: ToolHandler
    result_model: type[BaseModel] | None = None
    preconditions: tuple[str, ...] = ()
    side_effect: str | None = None
    idempotent: bool = False
    idempotency_key: str | None = None
    compensate_tool: str | None = None
    #: 补偿工具的入参怎么来(见 `CompensateArg`)。声明了 compensate_tool 就必须声明它 ——
    #: 否则「可补偿」只是一句口号,真到要回滚的时候没人知道该传什么参数。
    compensate_args: Mapping[str, CompensateArg] | None = None
    snapshot: ToolSnapshot | None = None
    reason_field: str | None = None
    # 声明「哪个参数是金额」。策略引擎据此套用额度(如 L4 单笔自主限额),
    # 没声明的工具在需要额度判断时只能走人工审批 —— 不猜字段名。
    amount_field: str | None = None
    # 参数里没给金额时,由工具自己按业务规则算出实际生效的金额。
    # 不声明就当作「金额未知」,从严审批 —— 猜一个数字比按未知处理更危险。
    amount_resolver: AmountResolver | None = None
    tags: tuple[str, ...] = ()

    @property
    def is_read_only(self) -> bool:
        return self.risk_level is RiskLevel.READ_ONLY

    def describe(self) -> dict[str, Any]:
        """给编排层(以及后续 MCP / function-calling)的完整工具说明。"""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "read_only": self.is_read_only,
            "parameters": self.params_model.model_json_schema(),
            "preconditions": list(self.preconditions),
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
            "idempotency_key": self.idempotency_key,
            "compensate_tool": self.compensate_tool,
            "compensate_args": (
                {
                    name: {"result_path": arg.result_path, "const": arg.const}
                    for name, arg in self.compensate_args.items()
                }
                if self.compensate_args
                else None
            ),
            # 让编排层/策略引擎知道这个工具能不能产出可读的审计 diff
            "audit_snapshot": self.snapshot is not None,
            "audit_reason_field": self.reason_field,
            "amount_field": self.amount_field,
            "tags": list(self.tags),
        }

    async def capture(self, context: ToolContext, params: BaseModel) -> dict[str, Any] | None:
        """取审计快照。只读工具不产生审计行,恒返回 None。"""
        if self.is_read_only or self.snapshot is None:
            return None
        return await self.snapshot(context, params)

    def extract_reason(self, params: BaseModel, arguments: Mapping[str, Any]) -> str | None:
        """从参数里取「为什么做这次操作」。审计要能自证动机,而不是只记结果。"""
        if self.reason_field is None:
            return None
        value = getattr(params, self.reason_field, None)
        if value is None:
            value = arguments.get(self.reason_field)
        return str(value) if value is not None else None
