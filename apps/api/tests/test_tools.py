"""工具层的声明与注册契约(纯逻辑,不需要数据库)。"""

import pytest
from pydantic import BaseModel, ConfigDict, Field

from guardrail_api.domain.errors import NotFound, ToolArgumentError
from guardrail_api.tools import RiskLevel, ToolContext, load_tools, registry
from guardrail_api.tools.base import CompensateArg, ToolSpec
from guardrail_api.tools.registry import ToolRegistry


class PingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: int = Field(ge=1, description="任意正整数")


class PingResult(BaseModel):
    value: int


async def ping_handler(context: ToolContext, params: PingParams) -> PingResult:
    return PingResult(value=params.value)


async def ping_snapshot(context: ToolContext, params: PingParams) -> dict:
    return {"value": params.value}


def test_all_tools_are_discovered_by_module_scan() -> None:
    tools = load_tools()

    assert tools.names() == [
        "close_ticket",
        "create_refund",
        "query_logistics",
        "query_order",
        "query_refundable",
    ]


def test_only_read_only_tools_have_no_side_effect() -> None:
    for spec in load_tools().all():
        if spec.is_read_only:
            assert spec.side_effect is None, f"{spec.name} 是只读工具却声明了副作用"
        else:
            assert spec.side_effect, f"{spec.name} 是写工具却没声明副作用"


def test_write_tool_must_declare_audit_snapshot() -> None:
    """写工具不声明审计快照就不允许接入 —— 没有 before/after 的写操作等于没发生过。"""
    for spec in load_tools().all():
        if not spec.is_read_only:
            assert spec.snapshot is not None, f"{spec.name} 是写工具却没声明审计快照"

    fresh = ToolRegistry()
    with pytest.raises(RuntimeError, match="必须声明审计快照"):
        fresh.register(
            ToolSpec(
                name="unobservable_write",
                title="不可观测的写",
                description="应该注册失败",
                risk_level=RiskLevel.LOW,
                params_model=PingParams,
                handler=ping_handler,
                side_effect="改了什么没人知道",
            )
        )


def test_high_risk_tool_declares_idempotency_and_compensation() -> None:
    spec = load_tools().get("create_refund")

    assert spec.risk_level is RiskLevel.HIGH
    assert spec.idempotent
    assert spec.idempotency_key == "hash(run_id, tool, args)"
    assert spec.compensate_tool == "close_ticket"
    assert spec.preconditions


def test_compensation_targets_are_registered() -> None:
    tools = load_tools()

    for spec in tools.all():
        if spec.compensate_tool:
            assert spec.compensate_tool in tools


def test_describe_exposes_llm_ready_json_schema() -> None:
    described = {item["name"]: item for item in load_tools().describe()}
    refund = described["create_refund"]

    schema = refund["parameters"]
    assert schema["additionalProperties"] is False, "未知参数必须被拒绝"
    assert schema["properties"]["reason_code"]["enum"] == [
        "QUALITY_ISSUE",
        "WRONG_ITEM",
        "DAMAGED_IN_TRANSIT",
        "LATE_DELIVERY",
        "NO_LONGER_NEEDED",
    ]
    assert refund["side_effect"]
    assert refund["compensate_tool"] == "close_ticket"
    assert described["query_order"]["read_only"] is True


def test_write_tool_without_side_effect_cannot_register() -> None:
    fresh = ToolRegistry()

    with pytest.raises(RuntimeError, match="必须声明 side_effect"):
        fresh.register(
            ToolSpec(
                name="dangerous_write",
                title="没声明副作用的写工具",
                description="应该注册失败",
                risk_level=RiskLevel.LOW,
                params_model=PingParams,
                handler=ping_handler,
            )
        )


def test_high_risk_tool_without_compensation_cannot_register() -> None:
    fresh = ToolRegistry()

    with pytest.raises(RuntimeError, match="必须声明补偿动作"):
        fresh.register(
            ToolSpec(
                name="risky_write",
                title="高风险无补偿",
                description="应该注册失败",
                risk_level=RiskLevel.HIGH,
                params_model=PingParams,
                handler=ping_handler,
                side_effect="动钱",
                idempotent=True,
                idempotency_key="hash(run_id, tool, args)",
                snapshot=ping_snapshot,
            )
        )


def test_read_only_tool_claiming_side_effect_is_rejected() -> None:
    fresh = ToolRegistry()

    with pytest.raises(RuntimeError, match="不应声明副作用"):
        fresh.register(
            ToolSpec(
                name="lying_read_only",
                title="说谎的只读工具",
                description="应该注册失败",
                risk_level=RiskLevel.READ_ONLY,
                params_model=PingParams,
                handler=ping_handler,
                side_effect="偷偷写库",
            )
        )


def test_broken_compensation_reference_fails_validation() -> None:
    fresh = ToolRegistry()
    fresh.register(
        ToolSpec(
            name="orphan_write",
            title="补偿指向不存在的工具",
            description="注册能过,但自检必须失败",
            risk_level=RiskLevel.HIGH,
            params_model=PingParams,
            handler=ping_handler,
            side_effect="动钱",
            idempotent=True,
            idempotency_key="hash(run_id, tool, args)",
            compensate_tool="not_registered_anywhere",
            # 补偿参数声明齐了,才能走到「补偿目标是否存在」这一步自检 ——
            # 少声明参数会先被注册期拦下,测不到这里想测的分支。
            compensate_args={"reason": CompensateArg(const="回滚")},
            snapshot=ping_snapshot,
        )
    )

    with pytest.raises(RuntimeError, match="未注册"):
        fresh.validate()


async def test_new_tool_works_without_touching_orchestration_layer() -> None:
    """新增工具只需要一份声明 —— 注册表、调用入口、describe 都不用改。"""
    fresh = ToolRegistry()
    fresh.register(
        ToolSpec(
            name="ping",
            title="连通性测试",
            description="只读探针",
            risk_level=RiskLevel.READ_ONLY,
            params_model=PingParams,
            result_model=PingResult,
            handler=ping_handler,
            tags=("debug",),
        )
    )

    context = ToolContext(session=None, actor="agent:test")  # type: ignore[arg-type]
    result = await fresh.invoke("ping", context, {"value": 7})

    assert isinstance(result, PingResult)
    assert result.value == 7
    assert fresh.describe()[0]["tags"] == ["debug"]


async def test_unknown_tool_raises_not_found() -> None:
    context = ToolContext(session=None, actor="agent:test")  # type: ignore[arg-type]

    with pytest.raises(NotFound) as excinfo:
        await registry.invoke("no_such_tool", context, {})

    assert "不存在" in str(excinfo.value)


async def test_invalid_arguments_raise_field_level_errors() -> None:
    load_tools()
    context = ToolContext(session=None, actor="agent:test")  # type: ignore[arg-type]

    with pytest.raises(ToolArgumentError) as excinfo:
        await registry.invoke("query_order", context, {"order_id": 0})

    error = excinfo.value
    assert error.code == "invalid_tool_arguments"
    assert error.context["tool"] == "query_order"
    assert [item["field"] for item in error.context["errors"]] == ["order_id"]


async def test_unknown_argument_is_rejected() -> None:
    load_tools()
    context = ToolContext(session=None, actor="agent:test")  # type: ignore[arg-type]

    with pytest.raises(ToolArgumentError) as excinfo:
        await registry.invoke("query_order", context, {"order_id": 1, "sql": "drop table orders"})

    errors = excinfo.value.context["errors"]
    assert [item["field"] for item in errors] == ["sql"]
