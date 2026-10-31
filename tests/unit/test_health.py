"""Unit tests for health and readiness endpoints."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import status
from httpx import AsyncClient

from app.db.session import get_db_session
from app.main import app


@pytest.mark.asyncio
async def test_health_check_returns_200(async_client: AsyncClient) -> None:
    """Verify that /health returns HTTP 200 with expected application metadata."""
    response = await async_client.get("/health")
    assert response.status_code == status.HTTP_200_OK

    data = response.json()
    assert data["status"] == "ok"
    assert "app" in data
    assert "env" in data


@pytest.mark.asyncio
async def test_readiness_check_success_mock(async_client: AsyncClient) -> None:
    """Verify that /ready returns HTTP 200 when database query succeeds."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 1
    mock_session.execute.return_value = mock_result

    async def override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        response = await async_client.get("/ready")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "ready"
        assert data["database"] == "connected"
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_readiness_check_unexpected_scalar_returns_503(async_client: AsyncClient) -> None:
    """Verify that /ready returns HTTP 503 when query returns unexpected value."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 0
    mock_session.execute.return_value = mock_result

    async def override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        response = await async_client.get("/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["database"] == "unexpected_result"
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_readiness_check_timeout_returns_503(async_client: AsyncClient) -> None:
    """Verify that /ready returns HTTP 503 when database query times out."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = TimeoutError("Query timed out")

    async def override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        response = await async_client.get("/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["database"] == "timeout"
    finally:
        app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.asyncio
async def test_readiness_check_failure_returns_503(async_client: AsyncClient) -> None:
    """Verify that /ready returns HTTP 503 when database connection is refused."""
    mock_session = AsyncMock()
    mock_session.execute.side_effect = ConnectionRefusedError("Database connection failed")

    async def override_get_db_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        response = await async_client.get("/ready")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["database"] == "disconnected"
        # Ensure no sensitive credentials or exception details are leaked
        assert "Database connection failed" not in response.text
        assert "password" not in response.text
    finally:
        app.dependency_overrides.pop(get_db_session, None)
