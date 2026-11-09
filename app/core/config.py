"""Application configuration management using Pydantic Settings."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/docplatform"
DEFAULT_DEV_JWT_SECRET_KEY = "dev_insecure_jwt_secret_key_change_in_production_min_32_chars"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_NAME: str = Field(
        default="Document Processing Platform",
        description="Name of the application",
    )
    APP_ENV: Literal["development", "testing", "staging", "production"] = Field(
        default="development",
        description="Current runtime environment",
    )
    DEBUG: bool = Field(
        default=False,
        description="Debug mode toggle",
    )
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Application logging level",
    )

    # API Server Configuration
    API_HOST: str = Field(
        default="0.0.0.0",
        description="Bind host for the API server",
    )
    API_PORT: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Bind port for the API server",
    )

    # Database Configuration (PostgreSQL with asyncpg)
    DATABASE_URL: str = Field(
        default=DEFAULT_DEV_DATABASE_URL,
        description="Async PostgreSQL connection string",
    )
    DB_POOL_SIZE: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Database connection pool size",
    )
    DB_MAX_OVERFLOW: int = Field(
        default=20,
        ge=0,
        le=100,
        description="Maximum overflow connections beyond pool size",
    )
    DB_POOL_TIMEOUT: float = Field(
        default=30.0,
        ge=1.0,
        description="Database connection acquisition timeout in seconds",
    )

    # JWT & Authentication Configuration
    JWT_SECRET_KEY: str = Field(
        default=DEFAULT_DEV_JWT_SECRET_KEY,
        description="Cryptographic secret key for signing JWT tokens",
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        description="Cryptographic algorithm for signing JWT tokens",
    )
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=30,
        ge=1,
        description="Lifetime of access tokens in minutes",
    )
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = Field(
        default=7,
        ge=1,
        description="Lifetime of refresh tokens in days",
    )
    JWT_ISSUER: str = Field(
        default="docplatform",
        description="JWT token issuer claim (iss)",
    )
    JWT_AUDIENCE: str = Field(
        default="docplatform-api",
        description="JWT token audience claim (aud)",
    )

    # Email Verification, Password Reset & SMTP Configuration
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = Field(
        default=30,
        ge=1,
        description="Lifetime of email verification tokens in minutes",
    )
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = Field(
        default=30,
        ge=1,
        description="Lifetime of password reset tokens in minutes",
    )
    SMTP_HOST: str = Field(
        default="localhost",
        description="SMTP server hostname for outbound email delivery",
    )
    SMTP_PORT: int = Field(
        default=1025,
        ge=1,
        le=65535,
        description="SMTP server port (e.g. 1025 for Mailpit, 587 for TLS, 465 for SSL)",
    )
    SMTP_USERNAME: str | None = Field(
        default=None,
        description="Optional SMTP authentication username",
    )
    SMTP_PASSWORD: str | None = Field(
        default=None,
        description="Optional SMTP authentication password",
    )
    SMTP_USE_TLS: bool = Field(
        default=False,
        description="Upgrade SMTP connection to TLS via STARTTLS",
    )
    SMTP_USE_SSL: bool = Field(
        default=False,
        description="Use implicit SSL/TLS for SMTP connection",
    )
    SMTP_FROM_EMAIL: str = Field(
        default="noreply@docplatform.local",
        description="Sender email address for outgoing system emails",
    )
    SMTP_FROM_NAME: str = Field(
        default="Document Processing Platform",
        description="Sender display name for outgoing system emails",
    )
    FRONTEND_BASE_URL: str = Field(
        default="http://localhost:3000",
        description="Base URL of frontend application for verification links",
    )

    # Storage Configuration (Phase 3.1)
    STORAGE_BACKEND: Literal["local"] = Field(
        default="local",
        description="Active storage backend identifier (currently: 'local')",
    )
    STORAGE_LOCAL_ROOT: str = Field(
        default="./storage_data",
        description="Base directory for local filesystem storage",
    )
    STORAGE_CHUNK_SIZE: int = Field(
        default=1048576,  # 1 MiB (1024 * 1024 bytes)
        ge=65536,  # min 64 KiB
        le=16777216,  # max 16 MiB
        description="Chunk size in bytes for streaming file read/write operations",
    )

    # Document Upload Configuration (Phase 3.3)
    # Note on tiered limits: MAX_UPLOAD_SIZE_BYTES controls API ingress acceptance.
    # PROCESSING_MAX_INPUT_SIZE_BYTES controls downstream processing eligibility.
    # Stored documents may be rejected at processing time if exceeding processing limits.
    MAX_UPLOAD_SIZE_BYTES: int = Field(
        default=20971520,  # 20 MiB (20 * 1024 * 1024 bytes)
        ge=1,  # minimum 1 byte
        le=524288000,  # maximum 500 MiB
        description=(
            "Maximum allowed document upload file size in bytes at API ingress (default: 20 MiB)"
        ),
    )

    # Document Processing Configuration (Phase 4.1)
    PROCESSING_MAX_INPUT_SIZE_BYTES: int = Field(
        default=20971520,  # 20 MiB (20 * 1024 * 1024 bytes)
        ge=1,
        le=524288000,
        description=(
            "Maximum document size in bytes eligible for CPU/memory-intensive processing "
            "(intentionally decoupled from upload limit; default: 20 MiB)"
        ),
    )
    PROCESSING_TIMEOUT_SECONDS: float = Field(
        default=60.0,
        ge=1.0,
        le=3600.0,
        description="Maximum processing execution duration in seconds before timing out",
    )
    PROCESSING_MAX_TEXT_SIZE_BYTES: int = Field(
        default=10485760,  # 10 MiB (10 * 1024 * 1024 bytes)
        ge=1024,
        le=104857600,  # 100 MiB
        description="Maximum extracted plain text size in bytes",
    )
    PROCESSING_MAX_PAGES: int = Field(
        default=1000,
        ge=1,
        le=50000,
        description="Maximum allowed page count in a single document for processing",
    )
    PROCESSING_MAX_IMAGE_PIXELS: int = Field(
        default=50000000,  # 50 Megapixels
        ge=1000,
        le=500000000,
        description="Maximum allowed image pixel count (width * height)",
    )
    PROCESSING_MAX_METADATA_SIZE_BYTES: int = Field(
        default=65536,  # 64 KiB
        ge=1024,
        le=1048576,  # 1 MiB
        description="Maximum size in bytes for extracted metadata payload",
    )

    def __init__(self, _env_file: str | None = ".env", **values: Any) -> None:
        """Initialize settings with optional env_file override."""
        super().__init__(_env_file=_env_file, **values)

    @field_validator("STORAGE_LOCAL_ROOT")
    @classmethod
    def validate_storage_local_root(cls, v: str) -> str:
        """Validate storage local root path format and prevent null bytes or empty path."""
        if not v or not v.strip():
            raise ValueError("STORAGE_LOCAL_ROOT must not be empty.")
        cleaned = v.strip()
        if "\0" in cleaned:
            raise ValueError("STORAGE_LOCAL_ROOT cannot contain null bytes.")
        return cleaned

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url_scheme(cls, v: str) -> str:
        """Ensure the database connection URL specifies the asyncpg dialect."""
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use 'postgresql+asyncpg://' driver scheme for async access."
            )
        return v

    @field_validator("FRONTEND_BASE_URL")
    @classmethod
    def validate_frontend_base_url_scheme(cls, v: str) -> str:
        """Ensure FRONTEND_BASE_URL is a valid absolute URL scheme."""
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "FRONTEND_BASE_URL must be a valid absolute URL starting with http:// or https://."
            )
        return v

    @model_validator(mode="after")
    def validate_smtp_configuration(self) -> "Settings":
        """Validate general SMTP settings across all environments."""
        if self.SMTP_USE_TLS and self.SMTP_USE_SSL:
            raise ValueError(
                "SMTP_USE_TLS and SMTP_USE_SSL are mutually exclusive and cannot both be True."
            )
        if (self.SMTP_USERNAME and not self.SMTP_PASSWORD) or (
            self.SMTP_PASSWORD and not self.SMTP_USERNAME
        ):
            raise ValueError(
                "SMTP_USERNAME and SMTP_PASSWORD must both be provided if either is configured."
            )
        return self

    @model_validator(mode="after")
    def validate_production_configuration(self) -> "Settings":
        """Enforce strict security constraints for production environments."""
        if self.APP_ENV == "production":
            if self.DEBUG:
                raise ValueError("DEBUG must be False in production environment.")
            if self.DATABASE_URL == DEFAULT_DEV_DATABASE_URL:
                raise ValueError(
                    "Default development DATABASE_URL must not be used in production. "
                    "Provide a secure production database connection string."
                )
            if self.JWT_SECRET_KEY == DEFAULT_DEV_JWT_SECRET_KEY:
                raise ValueError(
                    "Default development JWT_SECRET_KEY must not be used in production. "
                    "Provide a secure production JWT secret key."
                )
            if len(self.JWT_SECRET_KEY) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be at least 32 characters long in production."
                )
            if self.JWT_ALGORITHM.lower() == "none" or self.JWT_ALGORITHM not in [
                "HS256",
                "HS384",
                "HS512",
            ]:
                raise ValueError(
                    "JWT_ALGORITHM must be a secure HMAC algorithm (e.g., HS256) in production."
                )
            # Production SMTP and URL security checks
            dev_smtp_hosts = {"localhost", "127.0.0.1", "::1"}
            if self.SMTP_HOST.lower() in dev_smtp_hosts or self.SMTP_PORT == 1025:
                raise ValueError(
                    "Development SMTP settings (localhost/127.0.0.1 on port 1025) "
                    "cannot be used in production. "
                    "Configure a production SMTP server via SMTP_HOST and SMTP_PORT."
                )
            frontend_lower = self.FRONTEND_BASE_URL.lower()
            if not self.FRONTEND_BASE_URL.startswith("https://"):
                raise ValueError("FRONTEND_BASE_URL must use HTTPS in production.")
            if "localhost" in frontend_lower or "127.0.0.1" in frontend_lower:
                raise ValueError(
                    "FRONTEND_BASE_URL must not point to localhost/127.0.0.1 in production. "
                    "Configure the public production frontend application URL."
                )
            if self.SMTP_USERNAME and not (self.SMTP_USE_TLS or self.SMTP_USE_SSL):
                raise ValueError("Authenticated SMTP must use TLS or SSL encryption in production.")
            if (
                self.SMTP_FROM_EMAIL.endswith(".local")
                or self.SMTP_FROM_EMAIL == "noreply@example.com"
            ):
                raise ValueError(
                    "SMTP_FROM_EMAIL must be configured with a valid domain in production."
                )
            # Production storage root safety check
            unsafe_roots = {"/", "/etc", "/bin", "/boot", "/dev", "/usr", "/sys", "/proc"}
            normalized_root = self.STORAGE_LOCAL_ROOT.strip().rstrip("/\\")
            if normalized_root in unsafe_roots:
                raise ValueError(
                    "STORAGE_LOCAL_ROOT cannot be set to a critical system directory: "
                    f"{normalized_root}"
                )
        return self

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.APP_ENV == "production"

    @property
    def is_testing(self) -> bool:
        """Check if running in testing environment."""
        return self.APP_ENV == "testing"


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached settings instance (useful during testing)."""
    get_settings.cache_clear()
