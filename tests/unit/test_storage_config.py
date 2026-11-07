"""Unit tests for storage configuration, settings validation, factory, and FastAPI dependency."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.storage import (
    LocalStorage,
    clear_storage_cache,
    create_storage_backend,
    get_default_storage,
    get_storage,
)
from app.storage.exceptions import StorageConfigurationError


def test_default_storage_settings() -> None:
    """Verify default storage configuration settings."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.STORAGE_BACKEND == "local"
        assert settings.STORAGE_LOCAL_ROOT == "./storage_data"
        assert settings.STORAGE_CHUNK_SIZE == 1048576


def test_storage_settings_environment_override() -> None:
    """Verify custom environment variables override storage settings properly."""
    env = {
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_ROOT": "/custom/storage",
        "STORAGE_CHUNK_SIZE": "2097152",  # 2MB
    }
    with patch.dict(os.environ, env, clear=True):
        settings = Settings(_env_file=None)
        assert settings.STORAGE_BACKEND == "local"
        assert settings.STORAGE_LOCAL_ROOT == "/custom/storage"
        assert settings.STORAGE_CHUNK_SIZE == 2097152


def test_invalid_storage_backend_rejected() -> None:
    """Verify unsupported storage backend fails pydantic validation."""
    with patch.dict(os.environ, {"STORAGE_BACKEND": "s3_unsupported"}, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "STORAGE_BACKEND" in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_chunk_size",
    [
        "32768",  # 32KB (below 64KB minimum)
        "33554432",  # 32MB (above 16MB maximum)
        "0",
        "-1024",
    ],
)
def test_invalid_storage_chunk_size_rejected(invalid_chunk_size: str) -> None:
    """Verify chunk size outside acceptable bounds (64KB - 16MB) is rejected."""
    with patch.dict(os.environ, {"STORAGE_CHUNK_SIZE": invalid_chunk_size}, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "STORAGE_CHUNK_SIZE" in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_root",
    [
        "",
        "   ",
        "some/path\x00with/nullbyte",
    ],
)
def test_invalid_storage_local_root_rejected(invalid_root: str) -> None:
    """Verify empty or null-byte local root path is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(STORAGE_LOCAL_ROOT=invalid_root, _env_file=None)
    assert "STORAGE_LOCAL_ROOT" in str(exc_info.value)


def test_production_dangerous_root_rejected() -> None:
    """Verify dangerous system paths are rejected when APP_ENV is production."""
    env = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:strong_pass@prod_host:5432/prod_db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "587",
        "SMTP_FROM_EMAIL": "noreply@company.com",
        "FRONTEND_BASE_URL": "https://app.example.com",
        "STORAGE_LOCAL_ROOT": "/etc",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "STORAGE_LOCAL_ROOT cannot be set to a critical system directory" in str(
            exc_info.value
        )


def test_create_storage_backend_factory() -> None:
    """Verify create_storage_backend returns an initialized LocalStorage instance."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)
        backend = create_storage_backend(settings)
        assert isinstance(backend, LocalStorage)
        assert backend.chunk_size == settings.STORAGE_CHUNK_SIZE


def test_create_storage_backend_unsupported_raises_error() -> None:
    """Verify factory raises StorageConfigurationError on unexpected backend value."""
    mock_settings = MagicMock()
    mock_settings.STORAGE_BACKEND = "unsupported_storage"
    with pytest.raises(StorageConfigurationError):
        create_storage_backend(mock_settings)


def test_get_default_storage_and_cache_clearing() -> None:
    """Verify get_default_storage caches the instance and clear_storage_cache resets it."""
    clear_storage_cache()
    inst1 = get_default_storage()
    inst2 = get_default_storage()
    assert inst1 is inst2

    clear_storage_cache()
    inst3 = get_default_storage()
    assert inst3 is not inst1
    clear_storage_cache()


def test_get_storage_fastapi_dependency() -> None:
    """Verify get_storage retrieves backend from request.app.state.storage or fallback."""
    mock_storage = MagicMock()
    mock_request = MagicMock()
    mock_request.app.state.storage = mock_storage

    retrieved = get_storage(mock_request)
    assert retrieved is mock_storage

    # Fallback when request has no storage attribute
    mock_request_no_storage = MagicMock()
    del mock_request_no_storage.app.state.storage
    fallback_retrieved = get_storage(mock_request_no_storage)
    assert isinstance(fallback_retrieved, LocalStorage)
    clear_storage_cache()


async def test_lifespan_storage_startup_and_shutdown() -> None:
    """Verify lifespan initializes storage and closes resources cleanly."""
    from unittest.mock import AsyncMock

    from app.main import lifespan

    mock_app = MagicMock()
    mock_app.state = MagicMock()

    with (
        patch("app.main.create_storage_backend") as mock_create,
        patch("app.main.close_engine", new_callable=AsyncMock) as mock_close_db,
    ):
        mock_storage = AsyncMock()
        mock_create.return_value = mock_storage

        async with lifespan(mock_app):
            mock_storage.initialize.assert_awaited_once()
            assert mock_app.state.storage == mock_storage

        mock_storage.close.assert_awaited_once()
        mock_close_db.assert_awaited_once()


async def test_lifespan_storage_startup_failure_cleans_up() -> None:
    """Verify that if storage initialization fails at startup, close is called."""
    from unittest.mock import AsyncMock

    from app.main import lifespan

    mock_app = MagicMock()

    with patch("app.main.create_storage_backend") as mock_create:
        mock_storage = AsyncMock()
        mock_storage.initialize.side_effect = RuntimeError("Disk unavailable")
        mock_create.return_value = mock_storage

        with pytest.raises(RuntimeError, match="Disk unavailable"):
            async with lifespan(mock_app):
                pass

        mock_storage.close.assert_awaited_once()
