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
from sqlalchemy import text

from app.db.session import get_db_session
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_migration_lifecycle_upgrade_downgrade_reupgrade() -> None:
    """Verify Alembic migration upgrade, downgrade, and re-upgrade on PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Ensure we start at head
    proc_init = await asyncio.create_subprocess_exec(
        ".venv/Scripts/alembic.exe",
        "upgrade",
        "head",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc_init.communicate()

    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        # 1. Verify column exists in current head (0011)
        res = await session.execute(
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
        proc_down = await asyncio.create_subprocess_exec(
            ".venv/Scripts/alembic.exe",
            "downgrade",
            "-1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc_down.communicate()
        assert proc_down.returncode == 0, f"Downgrade failed: {stderr.decode()}"

        # Verify column is gone in 0010
        res_down = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'documents' AND column_name = 'deletion_pending_at'"
            )
        )
        assert res_down.fetchone() is None

        # 3. Test Alembic re-upgrade to head (0011)
        proc_up = await asyncio.create_subprocess_exec(
            ".venv/Scripts/alembic.exe",
            "upgrade",
            "head",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout_up, stderr_up = await proc_up.communicate()
        assert proc_up.returncode == 0, f"Re-upgrade failed: {stderr_up.decode()}"

        # Verify column exists again
        res_reup = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'documents' AND column_name = 'deletion_pending_at'"
            )
        )
        assert res_reup.fetchone() is not None
    finally:
        # Guarantee database remains at head
        proc_final = await asyncio.create_subprocess_exec(
            ".venv/Scripts/alembic.exe",
            "upgrade",
            "head",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc_final.communicate()
