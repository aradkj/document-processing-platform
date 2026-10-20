"""Unit tests for configuration loading and validation."""

import os
from unittest.mock import patch

from app.core.config import Settings, get_settings


def test_default_settings() -> None:
    """Verify default configuration values without external environment file."""
    settings = Settings(_env_file=None)
    assert settings.APP_NAME == "Document Processing Platform"
    assert settings.APP_ENV == "development"
    assert settings.DEBUG is False
    assert settings.LOG_LEVEL == "INFO"
    assert settings.API_PORT == 8000
    assert "asyncpg" in settings.DATABASE_URL


def test_environment_override() -> None:
    """Verify settings properly read from environment variables."""
    test_env = {
        "APP_NAME": "Custom OCR App",
        "APP_ENV": "production",
        "DEBUG": "true",
        "LOG_LEVEL": "WARNING",
        "API_PORT": "9000",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@dbhost:5432/customdb",
    }
    with patch.dict(os.environ, test_env, clear=True):
        custom_settings = Settings(_env_file=None)
        assert custom_settings.APP_NAME == "Custom OCR App"
        assert custom_settings.APP_ENV == "production"
        assert custom_settings.DEBUG is True
        assert custom_settings.LOG_LEVEL == "WARNING"
        assert custom_settings.API_PORT == 9000
        assert custom_settings.DATABASE_URL == "postgresql+asyncpg://user:pass@dbhost:5432/customdb"
        assert custom_settings.is_production is True
        assert custom_settings.is_testing is False


def test_get_settings_cached() -> None:
    """Verify get_settings returns the singleton cached instance."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
