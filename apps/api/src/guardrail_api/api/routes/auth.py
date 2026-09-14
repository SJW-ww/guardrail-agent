"""登录 / 登出 / 我是谁。"""

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from guardrail_api.api.deps import SESSION_COOKIE, SessionDep, Unauthenticated
from guardrail_api.auth import SESSION_TTL, authenticate, create_session, load_session
from guardrail_api.auth.service import revoke_session
from guardrail_api.config import get_settings
from guardrail_api.models.auth import Principal

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PrincipalView(BaseModel):
    """对外只暴露身份,**永远不回显口令散列**。"""

    username: str
    display_name: str
    role: str
    actor: str


class SessionResponse(BaseModel):
    principal: PrincipalView
    expires_at: str


def to_view(principal: Principal) -> PrincipalView:
    return PrincipalView(
        username=principal.username,
        display_name=principal.display_name,
        role=principal.role,
        actor=f"human:{principal.username}",
    )


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        # 本地是 http,带上 Secure 浏览器会把这个 cookie 直接丢掉
        secure=not settings.is_local,
        path="/",
    )


@router.post("/login", response_model=SessionResponse, summary="登录")
async def login(body: LoginRequest, response: Response, session: SessionDep) -> SessionResponse:
    principal = await authenticate(session, username=body.username, password=body.password)
    token, record = await create_session(session, principal)
    set_session_cookie(response, token)
    return SessionResponse(principal=to_view(principal), expires_at=record.expires_at.isoformat())


@router.post("/logout", status_code=204, summary="登出")
async def logout(request: Request, session: SessionDep) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await revoke_session(session, token)
    # 撤销的是服务端记录,清 cookie 只是顺手 —— 令牌已经不作数了
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=SessionResponse, summary="当前登录身份")
async def me(request: Request, session: SessionDep) -> SessionResponse:
    token = request.cookies.get(SESSION_COOKIE)
    loaded = await load_session(session, token) if token else None
    if loaded is None:
        raise Unauthenticated("未登录或会话已失效")
    principal, record = loaded
    return SessionResponse(principal=to_view(principal), expires_at=record.expires_at.isoformat())
