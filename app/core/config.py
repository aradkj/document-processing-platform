"""Application configuration management using Pydantic Settings."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/docplatform"


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

    def __init__(self, _env_file: str | None = ".env", **values: Any) -> None:
        """Initialize settings with optional env_file override."""
        super().__init__(_env_file=_env_file, **values)

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url_scheme(cls, v: str) -> str:
        """Ensure the database connection URL specifies the asyncpg dialect."""
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use 'postgresql+asyncpg://' driver scheme for async access."
            )
        return v

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
