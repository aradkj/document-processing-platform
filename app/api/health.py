"""Health and readiness check endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    status_code=status.HTTP_200_OK,
    summary="Application liveness check",
    description="Check whether the API service process is alive and responsive.",
)
async def health_check() -> dict[str, str]:
    """Return process health status without querying external dependencies."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "env": settings.APP_ENV,
    }


@router.get(
    "/ready",
    summary="Application readiness check",
    description="Check whether the application can communicate with its PostgreSQL database.",
)
async def readiness_check(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> JSONResponse:
    """Verify application readiness by executing a lightweight database query."""
    try:
        result = await session.execute(text("SELECT 1"))
        scalar = result.scalar()
        if scalar == 1:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "ready",
                    "database": "connected",
                },
            )
        else:
            logger.error("Readiness check received unexpected result: %s", scalar)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "unhealthy",
                    "database": "unexpected_result",
                },
            )
    except Exception as exc:
        # Redact and log error safely without leaking database credentials or traces to client
        logger.error("Readiness check failed to connect to database: %s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "database": "disconnected",
            },
        )
