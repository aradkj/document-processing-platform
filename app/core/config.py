"""Application configuration management using Pydantic Settings."""

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        description="Bind port for the API server",
    )

    # Database Configuration (PostgreSQL with asyncpg)
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/docplatform",
        description="Async PostgreSQL connection string",
    )

    def __init__(self, _env_file: str | None = ".env", **values: Any) -> None:
        """Initialize settings with optional env_file override."""
        super().__init__(_env_file=_env_file, **values)

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
