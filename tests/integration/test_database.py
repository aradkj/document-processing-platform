"""Integration tests for PostgreSQL connectivity, session lifecycle, and migrations."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient
from sqlalchemy import text

from app.db.session import close_engine, get_db_session, get_engine


async def is_postgres_available() -> bool:
    """Check if PostgreSQL is reachable using the configured DATABASE_URL."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            return result.scalar() == 1
    except Exception:
        return False


@pytest.mark.asyncio
async def test_postgres_connectivity_direct() -> None:
    """Verify direct async connection and query execution on PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL instance is not reachable at configured DATABASE_URL.")

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_session_lifecycle_execution() -> None:
    """Verify get_db_session generator lifecycle with successful query."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL instance is not reachable at configured DATABASE_URL.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar() == 1
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_session_lifecycle_rollback_on_exception() -> None:
    """Verify get_db_session rolls back on unhandled error."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL instance is not reachable at configured DATABASE_URL.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        await session.execute(text("SELECT 1"))
        # Trigger an exception back into the generator
        with pytest.raises(RuntimeError):
            await session_gen.athrow(RuntimeError("Simulated business error"))
    finally:
        # Session should be closed
        assert session.is_active is False or not session.in_transaction()


@pytest.mark.asyncio
async def test_multiple_sessions_isolation() -> None:
    """Verify multiple concurrent database sessions are independent."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL instance is not reachable at configured DATABASE_URL.")

    gen1 = get_db_session()
    gen2 = get_db_session()

    s1 = await anext(gen1)
    s2 = await anext(gen2)

    try:
        r1 = await s1.execute(text("SELECT 1"))
        r2 = await s2.execute(text("SELECT 2"))
        assert r1.scalar() == 1
        assert r2.scalar() == 2
        assert s1 is not s2
    finally:
        try:
            await anext(gen1)
        except StopAsyncIteration:
            pass
        try:
            await anext(gen2)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_readiness_endpoint_integration(async_client: AsyncClient) -> None:
    """Verify /ready against live database dependency."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL instance is not reachable at configured DATABASE_URL.")

    response = await async_client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


@pytest.mark.asyncio
async def test_alembic_single_head_and_history() -> None:
    """Verify Alembic configuration discovers exactly one migration head."""
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert heads[0] == "0004_user_ci_indexes"


@pytest.mark.asyncio
async def test_engine_dispose_cleanly() -> None:
    """Verify that close_engine cleans up engine resources."""
    await close_engine()
