"""Unit tests for configuration loading, validation, and cache management."""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings, clear_settings_cache, get_settings


def test_default_settings() -> None:
    """Verify default configuration values without external environment file or variables."""
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)
        assert settings.APP_NAME == "Document Processing Platform"
        assert settings.APP_ENV == "development"
        assert settings.DEBUG is False
        assert settings.LOG_LEVEL == "INFO"
        assert settings.API_HOST == "0.0.0.0"
        assert settings.API_PORT == 8000
        assert "postgresql+asyncpg://" in settings.DATABASE_URL
        assert settings.DB_POOL_SIZE == 10
        assert settings.DB_MAX_OVERFLOW == 20
        assert settings.DB_POOL_TIMEOUT == 30.0


def test_environment_override() -> None:
    """Verify settings properly read and cast from environment variables."""
    test_env = {
        "APP_NAME": "Custom OCR App",
        "APP_ENV": "staging",
        "DEBUG": "true",
        "LOG_LEVEL": "WARNING",
        "API_PORT": "9000",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@dbhost:5432/customdb",
        "DB_POOL_SIZE": "15",
        "DB_MAX_OVERFLOW": "25",
        "DB_POOL_TIMEOUT": "45.0",
    }
    with patch.dict(os.environ, test_env, clear=True):
        custom_settings = Settings(_env_file=None)
        assert custom_settings.APP_NAME == "Custom OCR App"
        assert custom_settings.APP_ENV == "staging"
        assert custom_settings.DEBUG is True
        assert custom_settings.LOG_LEVEL == "WARNING"
        assert custom_settings.API_PORT == 9000
        assert custom_settings.DATABASE_URL == "postgresql+asyncpg://user:pass@dbhost:5432/customdb"
        assert custom_settings.DB_POOL_SIZE == 15
        assert custom_settings.DB_MAX_OVERFLOW == 25
        assert custom_settings.DB_POOL_TIMEOUT == 45.0
        assert custom_settings.is_production is False
        assert custom_settings.is_testing is False


def test_invalid_database_url_scheme_raises_error() -> None:
    """Verify that a non-asyncpg database URL scheme is rejected."""
    bad_url = {"DATABASE_URL": "postgresql://user:pass@localhost:5432/db"}
    with patch.dict(os.environ, bad_url, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "postgresql+asyncpg://" in str(exc_info.value)


def test_invalid_api_port_raises_error() -> None:
    """Verify out-of-range API ports are rejected."""
    with patch.dict(os.environ, {"API_PORT": "70000"}, clear=True):
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    with patch.dict(os.environ, {"API_PORT": "0"}, clear=True):
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_production_mode_disallows_debug() -> None:
    """Verify that DEBUG=True in production environment is prohibited."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:strong_password@prod_db:5432/prod_platform",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "DEBUG must be False in production" in str(exc_info.value)


def test_production_mode_disallows_default_dev_db_url() -> None:
    """Verify that default development credentials cannot be used in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "Default development DATABASE_URL must not be used in production" in str(
            exc_info.value
        )


def test_valid_production_configuration() -> None:
    """Verify that valid production settings load successfully."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:super_secret_pw@db.internal:5432/prod_db",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        prod_settings = Settings(_env_file=None)
        assert prod_settings.is_production is True
        assert prod_settings.is_testing is False
        assert prod_settings.DEBUG is False


def test_testing_environment_property() -> None:
    """Verify is_testing returns True only when APP_ENV is testing."""
    with patch.dict(os.environ, {"APP_ENV": "testing"}, clear=True):
        s = Settings(_env_file=None)
        assert s.is_testing is True
        assert s.is_production is False


def test_get_settings_cached_and_clear_cache() -> None:
    """Verify get_settings cache behavior and clear_settings_cache function."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2

    clear_settings_cache()
    s3 = get_settings()
    assert s3 is not None
