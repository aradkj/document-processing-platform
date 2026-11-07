"""FastAPI application entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.v1 import api_v1_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import close_engine
from app.storage import create_storage_backend

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Handle application startup and shutdown events."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    logger.info(
        "Application starting up",
        extra={
            "app_name": settings.APP_NAME,
            "environment": settings.APP_ENV,
            "debug": settings.DEBUG,
        },
    )
    # Initialize storage backend
    storage = create_storage_backend(settings)
    try:
        await storage.initialize()
    except Exception:
        await storage.close()
        raise
    app.state.storage = storage
    logger.info("Storage backend '%s' initialized successfully", settings.STORAGE_BACKEND)
    try:
        yield
    finally:
        logger.info("Application shutting down, closing resources...")
        try:
            await storage.close()
        except Exception as exc:
            logger.error("Error closing storage backend: %s", exc)
        finally:
            await close_engine()
        logger.info("Application shutdown complete.")


OPENAPI_TAGS = [
    {
        "name": "Authentication",
        "description": (
            "User identity, session lifecycle, credential verification, and account recovery. "
            "Includes registration, login, JWT access/refresh token rotation, logout, "
            "email verification, profile management, and password reset."
        ),
    },
    {
        "name": "Documents",
        "description": (
            "Secure document upload, content-signature validation, bounded-memory streaming, "
            "and PostgreSQL metadata persistence."
        ),
    },
    {
        "name": "Health",
        "description": "Operational health probes for service liveness and database readiness.",
    },
]

API_DESCRIPTION = """
## Document Processing Platform API

A robust, production-conscious asynchronous backend service providing secure user identity,
authentication, and session management.

### Key Features (Phase 2):
* **Argon2id Password Security:** Memory-hard password hashing and validation policy.
* **Dual-Token JWT Architecture:** Short-lived access tokens (HS256) and persistent refresh tokens.
* **Refresh Token Rotation & Reuse Detection:** Automatic revocation of token families on reuse.
* **Instant Session Invalidation:** Global access token invalidation via persistent `auth_version`.
* **Email Verification & Password Reset:** High-entropy single-use tokens with URL hash fragments.
* **Anti-Enumeration Protection:** Uniform status codes and constant-time dummy hashing.
* **Zero Secret Leakage:** Strict Pydantic response filtering preventing credential leaks.
"""


def create_application() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description=API_DESCRIPTION,
        version="0.2.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        openapi_tags=OPENAPI_TAGS,
        contact={
            "name": "Document Processing Platform Engineering",
            "url": "https://github.com/aradkj/document-processing-platform",
        },
        license_info={
            "name": "Proprietary",
        },
        swagger_ui_parameters={
            "persistAuthorization": True,
            "displayRequestDuration": True,
        },
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch all unhandled exceptions to prevent stack trace leakage to API clients."""
        logger.exception(
            "Unhandled server error processing request: %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # Health and operational endpoints
    app.include_router(health_router)

    # Versioned API routes (Phase 2+)
    app.include_router(api_v1_router)

    return app


app = create_application()
