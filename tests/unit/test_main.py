"""Unit tests for FastAPI application lifecycle and global error handling."""

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient

from app.main import create_application


@pytest.mark.asyncio
async def test_application_creation_and_metadata() -> None:
    """Verify application factory creates configured FastAPI app."""
    test_app = create_application()
    assert test_app.title == "Document Processing Platform"


@pytest.mark.asyncio
async def test_global_unhandled_exception_handler() -> None:
    """Verify unhandled exceptions return 500 JSON without leaking stack traces."""
    test_app = create_application()

    @test_app.get("/trigger-unhandled-error")
    async def trigger_error() -> None:
        raise RuntimeError("Unexpected internal crash with secret_system_token_xyz")

    transport = ASGITransport(app=test_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/trigger-unhandled-error")
        assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        data = response.json()
        assert data["detail"] == "Internal server error"
        assert "secret_system_token_xyz" not in response.text
        assert "RuntimeError" not in response.text
        assert "Traceback" not in response.text
