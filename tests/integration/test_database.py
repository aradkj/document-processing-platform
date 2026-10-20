"""Integration tests for PostgreSQL connectivity and session lifecycle."""

import pytest
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
async def test_session_lifecycle_commit() -> None:
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
async def test_engine_dispose_cleanly() -> None:
    """Verify that close_engine cleans up engine resources."""
    await close_engine()
