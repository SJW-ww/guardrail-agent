"""身份与口令(不需要数据库)。"""

import pytest
from starlette.requests import Request

from guardrail_api.api.deps import DEFAULT_ACTOR, Unauthenticated, get_actor
from guardrail_api.auth import hash_password, verify_password
from guardrail_api.auth.service import hash_token
from guardrail_api.config import get_settings


class _UnusedSession:
    """没有 cookie 的路径不该碰数据库;真碰了就说明这里逻辑写歪了。"""

    async def execute(self, *_: object, **__: object) -> None:
        raise AssertionError("无 cookie 时不应访问数据库")


def make_request(cookie: str | None = None) -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_password_roundtrip() -> None:
    stored = hash_password("guardrail-demo")

    assert stored.startswith("scrypt$")
    assert "guardrail-demo" not in stored
    assert verify_password("guardrail-demo", stored)
    assert not verify_password("guardrail-demo ", stored)
    assert not verify_password("", stored)


def test_same_password_hashes_differently() -> None:
    """每次都要新盐,否则彩虹表能一次打穿所有同口令账号。"""
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected_not_raised() -> None:
    """脏数据不能把登录入口打成 500。"""
    for broken in ("", "plaintext", "scrypt$x$y$z$a$b", "bcrypt$1$2$3$4$5"):
        assert not verify_password("whatever", broken)


def test_old_parameters_still_verify() -> None:
    """参数升级后老口令必须还能用 —— 否则一升级就全员锁在门外。"""
    import base64
    import hashlib
    import secrets

    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(b"legacy", salt=salt, n=2**12, r=8, p=1, dklen=32)
    stored = (
        f"scrypt$4096$8$1${base64.b64encode(salt).decode()}${base64.b64encode(derived).decode()}"
    )

    assert verify_password("legacy", stored)


def test_token_hash_is_stable_and_not_the_token() -> None:
    token = "abc123"

    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert len(hash_token(token)) == 64


async def test_actor_falls_back_to_header_only_in_local_env() -> None:
    settings = get_settings()
    assert settings.is_local

    assert await get_actor(make_request(), _UnusedSession(), "finance-01") == "finance-01"
    assert await get_actor(make_request(), _UnusedSession(), None) == DEFAULT_ACTOR


async def test_prod_env_refuses_anonymous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    try:
        # 生产环境里 X-Actor 不再是身份来源:调用方不能自称是谁
        with pytest.raises(Unauthenticated):
            await get_actor(make_request(), _UnusedSession(), "human:supervisor-99")
        with pytest.raises(Unauthenticated):
            await get_actor(make_request(), _UnusedSession(), None)
    finally:
        get_settings.cache_clear()
