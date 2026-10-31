"""Async database engine and session lifecycle management."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Engine and sessionmaker singletons initialized on demand
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Acquire or create the shared async database engine with configured connection pool."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=settings.DEBUG and not settings.is_production,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Acquire or create the shared async sessionmaker."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def close_engine() -> None:
    """Dispose of the database engine connection pool."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database engine connection pool closed.")


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session with automatic lifecycle.

    Transaction Architecture Decision (Phase 1.5 Hardening):
    - Explicit Transaction Boundaries: Business layers or routes that mutate state
      should explicitly call `await session.commit()` upon successful operation.
    - Safety Against Accidental Commits: If a route encounters an error and returns
      a non-200 HTTP response without raising an exception, uncommitted changes
      are NOT blindly committed.
    - Guaranteed Rollback & Cleanup: Any uncommitted transaction is rolled back on
      exception, and the session is always closed in the finally block.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
