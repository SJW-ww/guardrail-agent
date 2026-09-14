"""登录 / 会话解析。

会话令牌是 `secrets.token_urlsafe` 出来的**不透明随机串**,库里只存 sha256。
认领会话时先算散列再查表 —— 即使数据库被读走,拿到的也只是散列。
"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from guardrail_api.auth.passwords import hash_password, verify_password
from guardrail_api.domain.errors import DomainError
from guardrail_api.models.auth import LoginSession, Principal

SESSION_TTL = timedelta(hours=12)
# 用户不存在时也跑一遍散列计算:响应时间不泄露"这个用户名存在吗"
_DUMMY_HASH = hash_password("not-a-real-password")


class InvalidCredentials(DomainError):
    """用户名或口令不对。

    刻意不区分「用户名不存在」和「口令错误」—— 分开报等于送给攻击者一个
    用户名字典。真正的人需要的是「登录失败」,审计需要的是「失败了几次」。
    """

    code = "invalid_credentials"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def find_principal(session: AsyncSession, username: str) -> Principal | None:
    result = await session.execute(select(Principal).where(Principal.username == username))
    return result.scalar_one_or_none()


async def authenticate(session: AsyncSession, *, username: str, password: str) -> Principal:
    principal = await find_principal(session, username)
    if principal is None:
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentials("用户名或口令不正确")

    if not verify_password(password, principal.password_hash):
        raise InvalidCredentials("用户名或口令不正确")
    if principal.disabled:
        raise InvalidCredentials("该账号已停用")
    return principal


async def create_session(
    session: AsyncSession, principal: Principal, *, ttl: timedelta = SESSION_TTL
) -> tuple[str, LoginSession]:
    from secrets import token_urlsafe

    token = token_urlsafe(32)
    record = LoginSession(
        token_hash=hash_token(token),
        principal_id=principal.id,
        expires_at=datetime.now(UTC) + ttl,
    )
    session.add(record)
    await session.commit()
    return token, record


async def load_session(session: AsyncSession, token: str) -> tuple[Principal, LoginSession] | None:
    """令牌 → (人, 会话)。过期或被撤销都返回 None —— 调用方决定是 401 还是退回本地模式。"""
    result = await session.execute(
        select(LoginSession, Principal)
        .join(Principal, Principal.id == LoginSession.principal_id)
        .where(LoginSession.token_hash == hash_token(token))
    )
    row = result.one_or_none()
    if row is None:
        return None
    record, principal = row
    if record.revoked_at is not None or record.expires_at <= datetime.now(UTC):
        return None
    if principal.disabled:
        return None
    return principal, record


async def resolve_session(session: AsyncSession, token: str) -> Principal | None:
    """只要人。鉴权用得上,前端要展示会话有效期时用 `load_session`。"""
    loaded = await load_session(session, token)
    return loaded[0] if loaded else None


async def revoke_session(session: AsyncSession, token: str) -> None:
    result = await session.execute(
        select(LoginSession).where(LoginSession.token_hash == hash_token(token))
    )
    record = result.scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return
    record.revoked_at = datetime.now(UTC)
    await session.commit()
