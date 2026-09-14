"""路由依赖:操作者身份从哪来。"""

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.auth import resolve_session
from guardrail_api.config import get_settings
from guardrail_api.db import get_session
from guardrail_api.domain.errors import DomainError

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# 登录态的载体。HttpOnly + SameSite=Lax:JS 读不到,跨站请求也带不出去。
SESSION_COOKIE = "guardrail_session"

# 本地/测试环境没登录时的兜底身份,方便命令行与端到端测试直接调接口。
DEFAULT_ACTOR = "operator-01"


class Unauthenticated(DomainError):
    code = "unauthenticated"


def normalize_actor(raw: str) -> str:
    """把操作者规范成**执行体身份**,和工具网关的 `human:` 口径对齐。

    已经带前缀的原样返回 —— `agent:` 前缀必须能被保留下来,
    否则策略引擎就认不出"这是机器在做决定"。这个区分是审批规则的前提。
    """
    return raw if ":" in raw else f"human:{raw}"


async def get_actor(
    request: Request,
    session: SessionDep,
    x_actor: Annotated[str | None, Header()] = None,
) -> str:
    """操作者来自**登录态**,不由调用方自称,更不由模型提供。

    优先认会话 cookie;cookie 无效时:

    - 非本地环境直接 401 —— 不能"登录过期了就当匿名",那等于悄悄把签字权交出去;
    - 本地/测试环境保留 `X-Actor`,让脚本与端到端测试能显式指定执行体。

    注意 `agent:` 这类机器身份**不能**通过登录获得:人只能以人的名义登录,
    机器执行体的身份由系统在推进 run 时自己写上。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        principal = await resolve_session(session, token)
        if principal is not None:
            return f"human:{principal.username}"

    if get_settings().is_local:
        return x_actor or DEFAULT_ACTOR

    raise Unauthenticated("请先登录")


ActorDep = Annotated[str, Depends(get_actor)]
