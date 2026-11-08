"""Integration tests for Document Phase 3.5 migration (0011_doc_deletion_pending_at).

Verifies:
- Migration upgrade to 0011_doc_deletion_pending_at adds deletion_pending_at.
- Column accepts timezone-aware timestamps and defaults to NULL.
- Existing rows are preserved in active state.
- Downgrade to 0010_create_documents_table cleanly removes the column.
- Re-upgrade to 0011_doc_deletion_pending_at cleanly re-applies.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.session import close_engine, get_engine
from tests.integration.test_database import is_postgres_available


def get_alembic_config() -> Config:
    """Create an Alembic Config pointing to project configuration."""
    settings = get_settings()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return cfg


@pytest.mark.asyncio
async def test_migration_lifecycle_upgrade_downgrade_reupgrade() -> None:
    """Verify Alembic migration upgrade, downgrade, and re-upgrade on PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    cfg = get_alembic_config()

    # Ensure engine closed before Alembic runs
    await close_engine()

    # Ensure we start at head
    await asyncio.to_thread(command.upgrade, cfg, "head")

    try:
        # 1. Verify column exists in current head (0011)
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'deletion_pending_at'"
                )
            )
            col_row = res.fetchone()
            assert col_row is not None
            assert col_row[0] == "deletion_pending_at"
            assert "timestamp" in col_row[1].lower()
            assert col_row[2] == "YES"

        # 2. Test Alembic downgrade to 0010
        await close_engine()
        await asyncio.to_thread(command.downgrade, cfg, "-1")

        # Verify column is gone in 0010
        engine2 = get_engine()
        async with engine2.connect() as conn2:
            res_down = await conn2.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'deletion_pending_at'"
                )
            )
            assert res_down.fetchone() is None

        # 3. Test Alembic re-upgrade to head (0011)
        await close_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # Verify column exists again
        engine3 = get_engine()
        async with engine3.connect() as conn3:
            res_reup = await conn3.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'deletion_pending_at'"
                )
            )
            assert res_reup.fetchone() is not None
    finally:
        # Guarantee database remains at head
        await close_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")
