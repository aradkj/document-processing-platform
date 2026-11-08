"""Integration tests for Document Alembic migration lifecycle against live PostgreSQL."""

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
async def test_migration_0010_upgrade_downgrade_reupgrade_cycle() -> None:
    """Verify clean downgrade to 0009 and re-upgrade to 0010 on PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    cfg = get_alembic_config()

    # 1. Close active engine before running Alembic migrations in separate thread/process
    await close_engine()

    # 2. Downgrade from 0010 to 0009 (runs in worker thread because alembic command is blocking)
    await asyncio.to_thread(command.downgrade, cfg, "0009_password_reset_tokens")

    try:
        # Verify documents table was dropped
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'documents'"
                    ");"
                )
            )
            assert res.scalar() is False, "documents table should have been dropped on downgrade"

            # Verify existing Phase 1 & 2 tables remain intact
            res_users = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'users'"
                    ");"
                )
            )
            assert res_users.scalar() is True, "users table must remain intact on downgrade"

            res_resets = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'password_reset_tokens'"
                    ");"
                )
            )
            assert res_resets.scalar() is True, "password_reset_tokens table must remain intact"

        await close_engine()

    finally:
        # 3. Re-upgrade to 0010
        await asyncio.to_thread(command.upgrade, cfg, "0010_create_documents_table")

    # 4. Verify documents table and constraints are restored
    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT FROM information_schema.tables "
                "  WHERE table_name = 'documents'"
                ");"
            )
        )
        assert res.scalar() is True, "documents table must exist after re-upgrade"

        # Verify check constraints restored
        res_ck = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "WHERE t.relname = 'documents' AND c.contype = 'c';"
            )
        )
        ck_names = {row[0] for row in res_ck.fetchall()}
        assert "ck_documents_size_bytes_non_negative" in ck_names
        assert "ck_documents_storage_key_hex32" in ck_names
        assert "ck_documents_sha256_hex64" in ck_names

        # Verify index restored
        res_idx = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'documents';")
        )
        idx_names = {row[0] for row in res_idx.fetchall()}
        assert "pk_documents" in idx_names
        assert "uq_documents_storage_key" in idx_names
        assert "ix_documents_user_id_created_at" in idx_names

    # 5. Restore database to Alembic head for subsequent tests
    await close_engine()
    await asyncio.to_thread(command.upgrade, cfg, "head")
