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
        assert settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS == 7


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
        "JWT_REFRESH_TOKEN_EXPIRE_DAYS": "14",
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
        assert custom_settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS == 14
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
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.internal.example.com",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "SMTP_FROM_EMAIL": "noreply@platform.example.com",
        "FRONTEND_BASE_URL": "https://app.example.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        prod_settings = Settings(_env_file=None)
        assert prod_settings.is_production is True
        assert prod_settings.is_testing is False
        expected_secret = "a_very_secure_and_long_production_jwt_secret_key_32_chars_min"
        assert prod_settings.JWT_SECRET_KEY == expected_secret
        assert prod_settings.SMTP_HOST == "smtp.internal.example.com"
        assert prod_settings.SMTP_USE_TLS is True


def test_production_mode_disallows_default_jwt_secret() -> None:
    """Verify that default JWT secret is rejected in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "Default development JWT_SECRET_KEY must not be used in production" in str(
            exc_info.value
        )


def test_production_mode_disallows_short_jwt_secret() -> None:
    """Verify that JWT secret shorter than 32 characters is rejected in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "short_secret_under_32",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "JWT_SECRET_KEY must be at least 32 characters long in production" in str(
            exc_info.value
        )


def test_production_mode_disallows_insecure_jwt_algorithm() -> None:
    """Verify that insecure JWT algorithm (e.g. none) is rejected in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "JWT_ALGORITHM": "none",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "JWT_ALGORITHM must be a secure HMAC algorithm" in str(exc_info.value)


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


def test_invalid_refresh_token_expire_days_rejected() -> None:
    """Verify that JWT_REFRESH_TOKEN_EXPIRE_DAYS < 1 is rejected by validation."""
    with patch.dict(os.environ, {"JWT_REFRESH_TOKEN_EXPIRE_DAYS": "0"}, clear=True):
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


def test_smtp_tls_and_ssl_mutually_exclusive() -> None:
    """Verify that enabling both SMTP_USE_TLS and SMTP_USE_SSL raises ValidationError."""
    env = {
        "SMTP_USE_TLS": "true",
        "SMTP_USE_SSL": "true",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "mutually exclusive" in str(exc_info.value)


def test_smtp_partial_credentials_rejected() -> None:
    """Verify that specifying only username without password (or vice versa) is rejected."""
    with patch.dict(os.environ, {"SMTP_USERNAME": "my_user"}, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "SMTP_USERNAME and SMTP_PASSWORD must both be provided" in str(exc_info.value)

    with patch.dict(os.environ, {"SMTP_PASSWORD": "my_password"}, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "SMTP_USERNAME and SMTP_PASSWORD must both be provided" in str(exc_info.value)


def test_production_mode_rejects_localhost_smtp() -> None:
    """Verify that production rejects localhost or port 1025 development SMTP settings."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "localhost",
        "SMTP_PORT": "1025",
        "FRONTEND_BASE_URL": "https://app.example.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "Development SMTP settings" in str(exc_info.value)


def test_production_mode_rejects_localhost_frontend_url() -> None:
    """Verify that production rejects localhost frontend base URL."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "FRONTEND_BASE_URL": "https://localhost:3000",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "FRONTEND_BASE_URL must not point to localhost" in str(exc_info.value)


def test_production_mode_rejects_local_from_email() -> None:
    """Verify that production rejects .local from_email domains."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "FRONTEND_BASE_URL": "https://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@docplatform.local",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "SMTP_FROM_EMAIL must be configured with a valid domain" in str(exc_info.value)


def test_production_mode_rejects_http_frontend_url() -> None:
    """Verify that non-HTTPS frontend URL is rejected in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "FRONTEND_BASE_URL": "http://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "FRONTEND_BASE_URL must use HTTPS in production" in str(exc_info.value)


def test_invalid_frontend_url_scheme_rejected() -> None:
    """Verify that invalid frontend URL schemes (e.g. ftp://) are rejected."""
    with patch.dict(os.environ, {"FRONTEND_BASE_URL": "ftp://app.company.com"}, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "starting with http:// or https://" in str(exc_info.value)


def test_production_authenticated_smtp_without_encryption_rejected() -> None:
    """Verify that specifying SMTP credentials without TLS or SSL is rejected in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "25",
        "SMTP_USE_TLS": "false",
        "SMTP_USE_SSL": "false",
        "SMTP_USERNAME": "smtp_user",
        "SMTP_PASSWORD": "smtp_password",
        "FRONTEND_BASE_URL": "https://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)
        assert "Authenticated SMTP must use TLS or SSL encryption in production" in str(
            exc_info.value
        )


def test_production_authenticated_smtp_with_starttls_accepted() -> None:
    """Verify that SMTP credentials with STARTTLS are accepted in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "SMTP_USE_SSL": "false",
        "SMTP_USERNAME": "smtp_user",
        "SMTP_PASSWORD": "smtp_password",
        "FRONTEND_BASE_URL": "https://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        s = Settings(_env_file=None)
        assert s.SMTP_USE_TLS is True
        assert s.SMTP_USERNAME == "smtp_user"


def test_production_authenticated_smtp_with_ssl_accepted() -> None:
    """Verify that SMTP credentials with SSL are accepted in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "smtp.company.com",
        "SMTP_PORT": "465",
        "SMTP_USE_TLS": "false",
        "SMTP_USE_SSL": "true",
        "SMTP_USERNAME": "smtp_user",
        "SMTP_PASSWORD": "smtp_password",
        "FRONTEND_BASE_URL": "https://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        s = Settings(_env_file=None)
        assert s.SMTP_USE_SSL is True
        assert s.SMTP_USERNAME == "smtp_user"


def test_production_credential_less_smtp_accepted() -> None:
    """Verify that unauthenticated internal relay without TLS is accepted in production."""
    prod_env = {
        "APP_ENV": "production",
        "DEBUG": "false",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:pw@db.internal:5432/db",
        "JWT_SECRET_KEY": "a_very_secure_and_long_production_jwt_secret_key_32_chars_min",
        "SMTP_HOST": "relay.internal.company.com",
        "SMTP_PORT": "25",
        "SMTP_USE_TLS": "false",
        "SMTP_USE_SSL": "false",
        "FRONTEND_BASE_URL": "https://app.company.com",
        "SMTP_FROM_EMAIL": "noreply@company.com",
    }
    with patch.dict(os.environ, prod_env, clear=True):
        s = Settings(_env_file=None)
        assert s.SMTP_HOST == "relay.internal.company.com"
        assert s.SMTP_USERNAME is None
