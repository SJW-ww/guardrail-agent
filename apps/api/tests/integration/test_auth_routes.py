"""登录链路:口令 → 会话 → 审计里的 actor。"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.api.deps import SESSION_COOKIE
from guardrail_api.auth import hash_password
from guardrail_api.models import Principal

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse"


async def make_principal(
    session: AsyncSession, *, username: str = "supervisor-01", role: str = "supervisor"
) -> Principal:
    principal = Principal(
        username=username,
        display_name=username,
        role=role,
        password_hash=hash_password(PASSWORD),
    )
    session.add(principal)
    await session.commit()
    return principal


async def test_login_sets_cookie_and_me_returns_identity(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    await make_principal(session)

    response = await api_client.post(
        "/auth/login", json={"username": "supervisor-01", "password": PASSWORD}
    )

    assert response.status_code == 200, response.text
    assert response.json()["principal"]["actor"] == "human:supervisor-01"
    assert SESSION_COOKIE in api_client.cookies

    me = await api_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["principal"]["role"] == "supervisor"


async def test_wrong_password_is_401_without_leaking_which_field(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    await make_principal(session)

    wrong_password = await api_client.post(
        "/auth/login", json={"username": "supervisor-01", "password": "nope"}
    )
    unknown_user = await api_client.post(
        "/auth/login", json={"username": "nobody", "password": PASSWORD}
    )

    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    # 「用户不存在」和「口令错误」必须长得一模一样
    assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]


async def test_me_without_cookie_is_401(api_client: AsyncClient) -> None:
    response = await api_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_logout_revokes_the_token_server_side(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    await make_principal(session)
    await api_client.post("/auth/login", json={"username": "supervisor-01", "password": PASSWORD})
    stolen = api_client.cookies[SESSION_COOKIE]

    logout = await api_client.post("/auth/logout")
    assert logout.status_code == 204
    assert (await api_client.get("/auth/me")).status_code == 401

    # 关键:清 cookie 只是客户端行为 —— 令牌本身必须在服务端失效,
    # 否则被复制的 cookie 还能继续以这个人的名义签字
    api_client.cookies.set(SESSION_COOKIE, stolen)
    assert (await api_client.get("/auth/me")).status_code == 401


async def test_disabled_principal_cannot_login(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    principal = await make_principal(session, username="leaver")
    principal.disabled = True
    await session.commit()

    response = await api_client.post(
        "/auth/login", json={"username": "leaver", "password": PASSWORD}
    )

    assert response.status_code == 401


async def test_logged_in_actor_wins_over_header(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """有了会话,X-Actor 就不再是身份来源 —— 否则伪造一个请求头就能变成别人。"""
    await make_principal(session, username="supervisor-01")
    await make_principal(session, username="finance-01", role="finance")
    await api_client.post("/auth/login", json={"username": "finance-01", "password": PASSWORD})

    created = await api_client.post(
        "/api/runs",
        json={
            "goal": "身份优先于请求头",
            "steps": [{"seq": 1, "tool": "query_order", "args": {"order_no": "NO-SUCH-ORDER"}}],
        },
        headers={"X-Actor": "human:supervisor-01"},
    )

    assert created.status_code == 200, created.text
    # 冒充主管的那个请求头被忽略,记下来的是登录的 finance-01
    assert created.json()["actor"] == "human:finance-01"
