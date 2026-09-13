import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://guardrail:guardrail@localhost:5432/guardrail"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from guardrail_api.main import app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """不启动 lifespan(不连 Redis / DB),只测路由与契约。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
