"""健康检查。/health 不碰外部依赖;/ready 检查 Postgres 与 Redis。"""

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.db import get_session

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    service: str


class ReadyResponse(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/health", response_model=HealthResponse, summary="存活探针")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="guardrail-api")


@router.get("/ready", response_model=ReadyResponse, summary="就绪探针")
async def ready(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> ReadyResponse:
    checks: dict[str, str] = {}

    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"

    all_ok = all(value == "ok" for value in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(status="ok" if all_ok else "degraded", checks=checks)
