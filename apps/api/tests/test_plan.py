"""多步计划:顺序依赖 + 参数引用。

这两件事都必须能**脱离数据库**单独验证 —— 计划不合法就该在登记阶段被挡住,
而不是跑了两步之后才发现第三步依赖的东西永远不会来。
"""

import pytest

from guardrail_api.domain.errors import PlanError
from guardrail_api.governance.executor import PlannedStep
from guardrail_api.governance.plan import (
    iter_refs,
    parse_ref,
    resolve,
    unresolved_seqs,
    validate,
)
from guardrail_api.tools import load_tools


def _step(seq: int, tool: str = "query_order", args: dict | None = None, **kwargs) -> PlannedStep:
    return PlannedStep(seq=seq, tool=tool, args=args or {}, **kwargs)


# ---------- 引用语法 ----------


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("1.ticket_id", (1, ["ticket_id"])),
        ("2.items[0].order_item_id", (2, ["items", 0, "order_item_id"])),
        ("3.customer.tier", (3, ["customer", "tier"])),
    ],
)
def test_reference_paths_are_parsed(ref: str, expected: tuple[int, list]) -> None:
    assert parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["ticket_id", "1.", "0-1.ticket_id", "1.ticket_id; drop", "$1.x"])
def test_invalid_reference_is_rejected(ref: str) -> None:
    with pytest.raises(PlanError):
        parse_ref(ref)


def test_refs_are_found_in_nested_arguments() -> None:
    args = {"a": {"$ref": "1.x"}, "b": [{"$ref": "1.y"}, "普通字符串"]}

    assert sorted(iter_refs(args)) == ["1.x", "1.y"]


def test_unresolved_seqs_only_covers_steps_with_refs() -> None:
    steps = [_step(1), _step(2, args={"amount_cents": {"$ref": "1.total_amount_cents"}})]

    assert unresolved_seqs(steps) == {2}


# ---------- 取值 ----------


def test_resolve_substitutes_scalars() -> None:
    args = {"order_id": 7, "amount_cents": {"$ref": "1.total_amount_cents"}}

    assert resolve(args, {1: {"total_amount_cents": 12_300}}) == {
        "order_id": 7,
        "amount_cents": 12_300,
    }


def test_resolve_walks_lists_and_nested_objects() -> None:
    results = {1: {"items": [{"order_item_id": 42}], "customer": {"tier": "VIP"}}}

    assert resolve({"item": {"$ref": "1.items[0].order_item_id"}}, results) == {"item": 42}
    assert resolve({"tier": {"$ref": "1.customer.tier"}}, results) == {"tier": "VIP"}


def test_resolve_refuses_to_move_whole_structures() -> None:
    """引用整个 dict 等于绕过下游工具的参数校验 —— 只允许标量。"""
    with pytest.raises(PlanError, match="只允许引用标量"):
        resolve({"items": {"$ref": "1.items"}}, {1: {"items": [1, 2]}})


def test_resolve_fails_loudly_when_the_upstream_has_not_succeeded() -> None:
    with pytest.raises(PlanError, match="还没有成功"):
        resolve({"amount_cents": {"$ref": "1.total_amount_cents"}}, {})


def test_resolve_reports_which_path_is_missing() -> None:
    with pytest.raises(PlanError) as excinfo:
        resolve({"x": {"$ref": "1.nope"}}, {1: {"total_amount_cents": 1}})

    assert excinfo.value.context["available"] == ["total_amount_cents"]


def test_resolve_index_out_of_range_is_an_error_not_none() -> None:
    with pytest.raises(PlanError, match="列表长度 1"):
        resolve({"x": {"$ref": "1.items[3].id"}}, {1: {"items": [{"id": 1}]}})


# ---------- 计划校验 ----------


def _valid_plan() -> list[PlannedStep]:
    return [
        _step(1, args={"order_id": 1}),
        _step(
            2,
            tool="create_refund",
            args={"order_id": 1, "amount_cents": {"$ref": "1.total_amount_cents"}},
            depends_on=(1,),
        ),
    ]


def test_a_well_formed_plan_passes() -> None:
    validate(_valid_plan(), registry=load_tools())


def test_plan_must_be_empty_checked() -> None:
    with pytest.raises(PlanError, match="不能为空"):
        validate([], registry=load_tools())


def test_duplicate_seq_is_rejected() -> None:
    with pytest.raises(PlanError, match="唯一"):
        validate([_step(1), _step(1)], registry=load_tools())


def test_steps_must_be_sorted_by_seq() -> None:
    with pytest.raises(PlanError, match="递增"):
        validate([_step(2, tool="create_refund", args={"order_id": 1}), _step(1)], registry=None)


def test_dependency_must_point_backwards() -> None:
    """依赖只能指向更早的步骤 —— 这条规则让「无环」成为构造性事实。"""
    plan = [_step(1, depends_on=(2,)), _step(2)]

    with pytest.raises(PlanError, match="更早"):
        validate(plan, registry=None)


def test_dependency_on_a_missing_step_is_rejected() -> None:
    with pytest.raises(PlanError, match="不存在"):
        validate([_step(1, depends_on=(9,))], registry=None)


def test_reference_to_a_later_step_is_rejected() -> None:
    plan = [_step(1, args={"x": {"$ref": "2.y"}}), _step(2)]

    with pytest.raises(PlanError, match="更早"):
        validate(plan, registry=None)


def test_unknown_tool_is_rejected_before_anything_runs() -> None:
    with pytest.raises(PlanError, match="未注册的工具"):
        validate([_step(1, tool="drop_database")], registry=load_tools())
