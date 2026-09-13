"""领域错误 → HTTP 状态码的映射。领域层不认识 HTTP,映射只在一处做。"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from guardrail_api.domain.errors import (
    DomainError,
    InsufficientStock,
    InvalidStateTransition,
    NotFound,
    ToolArgumentError,
)
from guardrail_api.main import create_app


def _app_raising(error: DomainError) -> FastAPI:
    app = create_app()

    @app.get("/__probe")
    async def probe() -> None:
        raise error

    return app


async def _call(error: DomainError) -> tuple[int, dict]:
    transport = ASGITransport(app=_app_raising(error))
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/__probe")
    return response.status_code, response.json()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (NotFound("订单 1 不存在"), 404),
        (InvalidStateTransition("订单不允许取消"), 409),
        (InsufficientStock("库存不足"), 409),
        (DomainError("兜底错误"), 400),
    ],
)
async def test_domain_error_maps_to_status(error: DomainError, expected_status: int) -> None:
    status_code, body = await _call(error)

    assert status_code == expected_status
    assert body["error"]["code"] == error.code
    assert body["error"]["message"] == error.message


async def test_tool_argument_error_returns_422_with_field_errors() -> None:
    error = ToolArgumentError(
        "工具 query_order 参数校验失败",
        tool="query_order",
        errors=[
            {
                "field": "order_id",
                "message": "Input should be >= 1",
                "type": "greater_than_equal",
            }
        ],
    )

    status_code, body = await _call(error)

    assert status_code == 422
    assert body["error"]["code"] == "invalid_tool_arguments"
    assert body["error"]["context"]["errors"][0]["field"] == "order_id"
