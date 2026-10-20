"""FastAPI application entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1 import api_v1_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import close_engine

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
    try:
        yield
    finally:
        logger.info("Application shutting down, closing resources...")
        await close_engine()
        logger.info("Application shutdown complete.")


def create_application() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description="Production-oriented backend for the Document Processing Platform",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # Health and operational endpoints
    app.include_router(health_router)

    # Versioned API routes (Phase 2+)
    app.include_router(api_v1_router)

    return app


app = create_application()
