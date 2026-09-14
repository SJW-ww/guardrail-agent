"""应用入口。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from guardrail_api import __version__
from guardrail_api.api.routes import audit, auth, health, orders, planner, runs, tickets
from guardrail_api.api.routes import tools as tools_routes
from guardrail_api.config import get_settings
from guardrail_api.db import dispose_engine
from guardrail_api.domain.errors import DomainError
from guardrail_api.governance import trace
from guardrail_api.tools import load_tools

logger = logging.getLogger("guardrail")

# 领域错误 → HTTP 状态码。领域层不认识 HTTP,映射只在这一个地方做。
STATUS_BY_ERROR_CODE = {
    "not_found": 404,
    "invalid_state_transition": 409,
    "insufficient_stock": 409,
    "rule_violation": 422,
    "invalid_tool_arguments": 422,
    "lease_lost": 409,
    "run_budget_exceeded": 422,
    "proposal_rejected": 422,
    # 规划器不可用是**服务端问题**,不是用户把话说错了 —— 所以不是 4xx
    "planner_unavailable": 503,
    # 不是「你说错了」,是「你不能做这件事」
    "policy_denied": 403,
    # 没登录、会话失效、口令不对 —— 都是「先证明你是谁」
    "unauthenticated": 401,
    "invalid_credentials": 401,
}
DEFAULT_ERROR_STATUS = 400


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=STATUS_BY_ERROR_CODE.get(exc.code, DEFAULT_ERROR_STATUS),
            content=jsonable_encoder(
                {
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "context": exc.context,
                    }
                }
            ),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    logger.info("starting %s (env=%s)", settings.app_name, settings.app_env)

    # 工具在启动期加载并自检:声明不合规就起不来,而不是等到第一次调用才炸
    tools = load_tools()
    logger.info("tools loaded (%d): %s", len(tools), ", ".join(tools.names()))
    app.state.tools = tools

    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await dispose_engine()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="GuardRail API",
        description="Agent 写操作安全执行层:模型只提议,系统做决策。",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router)
    app.include_router(orders.router)
    app.include_router(tickets.router)
    app.include_router(tools_routes.router)
    app.include_router(planner.router)
    app.include_router(runs.router)
    app.include_router(audit.router)

    @app.middleware("http")
    async def trace_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """trace_id 从入口贯穿到审计行。

        调用方可以带 `X-Trace-Id` 把跨系统的链路接上;不带就由这里生成。
        同一个 trace_id 会出现在这次请求触发的每一条审计记录里 ——
        事故复盘时不用靠时间戳猜哪几条是同一件事。
        """
        trace_id = request.headers.get("X-Trace-Id") or trace.new_trace_id()
        token = trace.set_trace_id(trace_id)
        try:
            response = await call_next(request)
        finally:
            trace.reset_trace_id(token)
        response.headers["X-Trace-Id"] = trace_id
        return response

    return app


app = create_app()
