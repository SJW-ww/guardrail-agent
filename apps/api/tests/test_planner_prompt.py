"""规划层里不需要数据库与模型服务的部分:JSON 提取、提示词渲染、后端分发。"""

import pytest

from guardrail_api.config import Settings
from guardrail_api.planner import planner_backend
from guardrail_api.planner.prompts import build_user_prompt, extract_json


@pytest.mark.parametrize(
    "raw",
    [
        '{"action": "query_order"}',
        '```json\n{"action": "query_order"}\n```',
        '```\n{"action": "query_order"}\n```',
        '好的,这是结果:\n{"action": "query_order"}\n还需要别的吗?',
    ],
)
def test_extract_json_tolerates_common_wrapping(raw: str) -> None:
    assert extract_json(raw) == {"action": "query_order"}


def test_extract_json_rejects_content_without_object() -> None:
    with pytest.raises(ValueError):
        extract_json("抱歉,我无法完成这个请求。")


def test_build_user_prompt_includes_intent_context_and_tools() -> None:
    prompt = build_user_prompt(
        intent="帮我退 80 元",
        context={"order_id": 1, "available_refund_cents": 8000},
        tools=[{"name": "create_refund", "parameters": {"type": "object"}}],
    )

    assert "帮我退 80 元" in prompt
    assert '"available_refund_cents": 8000' in prompt
    assert '"name": "create_refund"' in prompt


def test_planner_backend_falls_back_to_deterministic_without_api_key() -> None:
    assert Settings(planner_backend="llm", llm_api_key="").llm_configured is False
    assert Settings(planner_backend="deterministic", llm_api_key="sk-x").llm_configured is False
    assert Settings(planner_backend="llm", llm_api_key="sk-x").llm_configured is True


def test_planner_backend_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from guardrail_api import planner

    monkeypatch.setattr(
        planner, "get_settings", lambda: Settings(planner_backend="llm", llm_api_key="sk-x")
    )
    assert planner_backend() == "llm"

    monkeypatch.setattr(planner, "get_settings", lambda: Settings(planner_backend="deterministic"))
    assert planner_backend() == "deterministic"
