"""集成测试夹具。

需要真实 Postgres(库存行锁、CHECK 约束、`FOR UPDATE` 都是数据库行为,
用 SQLite 测不出来)。未设置 `TEST_DATABASE_URL` 时整体跳过,
所以默认的快速测试套件不受影响。
"""

import os
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from guardrail_api import models  # noqa: F401  让 Base.metadata 拿到全部表
from guardrail_api.db import Base, get_session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


async def _ensure_database(url: str) -> None:
    parsed = sa.engine.make_url(url)
    if not parsed.database:
        raise RuntimeError("TEST_DATABASE_URL 必须带库名")

    admin_engine = create_async_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        exists = await connection.scalar(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
        )
        if not exists:
            await connection.execute(sa.text(f'CREATE DATABASE "{parsed.database}"'))
    await admin_engine.dispose()


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    if not TEST_DATABASE_URL:
        pytest.skip("未设置 TEST_DATABASE_URL,跳过集成测试")

    await _ensure_database(TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session
        await db_session.rollback()


@pytest.fixture
async def api_client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """把应用的会话依赖指向测试库,否则路由会连到主库去。"""
    from guardrail_api import db as db_module
    from guardrail_api.main import app

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    # 执行器会自己开事务(每一步一次提交),它拿的是 db 模块里的全局会话工厂。
    # 不一起换掉的话,「写操作走执行器」这条路由会打到真正的开发库上 —— 测试全绿但数据脏了。
    original_factory = db_module._session_factory
    db_module._session_factory = factory

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with factory() as test_session:
            try:
                yield test_session
            except Exception:
                await test_session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        db_module._session_factory = original_factory
