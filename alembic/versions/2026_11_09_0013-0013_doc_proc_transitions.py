"""Tighten lifecycle check constraints and enforce state transitions via trigger.

Revision ID: 0013_doc_proc_transitions
Revises: 0012_doc_processing_lifecycle
Create Date: 2026-11-09 19:54:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_doc_proc_transitions"
down_revision: str | None = "0012_doc_processing_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_FN_SQL = """
CREATE OR REPLACE FUNCTION trg_fn_document_processings_transitions()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Prevent transitions out of terminal states (succeeded, failed, cancelled)
        IF OLD.status IN ('succeeded', 'failed', 'cancelled') AND NEW.status <> OLD.status THEN
            RAISE EXCEPTION
                'Illegal status transition for document_processing % from terminal state % to %',
                OLD.id, OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        -- Validate pending transitions: pending -> (pending, running, cancelled)
        IF OLD.status = 'pending' AND NEW.status NOT IN ('pending', 'running', 'cancelled') THEN
            RAISE EXCEPTION 'Illegal status transition for document_processing % from pending to %',
                OLD.id, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;

        -- Validate running transitions: running -> (running, succeeded, failed, cancelled)
        IF OLD.status = 'running'
           AND NEW.status NOT IN ('running', 'succeeded', 'failed', 'cancelled') THEN
            RAISE EXCEPTION 'Illegal status transition for document_processing % from running to %',
                OLD.id, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    """Migrate legacy data, tighten status check constraints, and enforce terminal immutability."""
    # 1. Migrate legacy 0012 records that would violate the tightened constraints.
    # To ensure no meaningful data is lost and no processing records are deleted,
    # preserve any existing result or error_code in error_message before nulling them.
    op.execute(
        """
        UPDATE document_processings
        SET
            error_message = CASE
                WHEN result IS NOT NULL AND result != 'null'::jsonb AND error_message IS NOT NULL
                    THEN error_message || ' | [migrated_result]: ' || result::text
                WHEN result IS NOT NULL AND result != 'null'::jsonb
                    THEN '[migrated_result]: ' || result::text
                ELSE error_message
            END,
            result = NULL
        WHERE status IN ('failed', 'cancelled')
          AND result IS NOT NULL AND result != 'null'::jsonb;
        """
    )
    op.execute(
        """
        UPDATE document_processings
        SET
            error_message = CASE
                WHEN error_message IS NOT NULL
                    THEN error_message || ' | [migrated_error_code]: ' || error_code
                ELSE '[migrated_error_code]: ' || error_code
            END,
            error_code = NULL
        WHERE status = 'cancelled'
          AND error_code IS NOT NULL;
        """
    )

    # 2. Update check constraint to strictly forbid results in failed/cancelled records
    op.execute(
        "ALTER TABLE document_processings "
        "DROP CONSTRAINT IF EXISTS ck_document_processings_status_fields;"
    )
    op.execute(
        """
        ALTER TABLE document_processings ADD CONSTRAINT ck_document_processings_status_fields
        CHECK (
            (status = 'pending' AND started_at IS NULL AND finished_at IS NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR
            (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR
            (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL
             AND result IS NOT NULL AND result != 'null'::jsonb AND error_code IS NULL) OR
            (status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NOT NULL) OR
            (status = 'cancelled' AND finished_at IS NOT NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL)
        );
        """
    )

    # 3. Create or replace trigger function and trigger
    op.execute(TRIGGER_FN_SQL)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_processings_transitions ON document_processings;"
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_processings_transitions
            BEFORE UPDATE ON document_processings
            FOR EACH ROW
            EXECUTE FUNCTION trg_fn_document_processings_transitions();
        """
    )


def downgrade() -> None:
    """Drop transition enforcement trigger and restore previous check constraint."""
    # 1. Drop trigger and function
    op.execute(
        "DROP TRIGGER IF EXISTS trg_document_processings_transitions ON document_processings;"
    )
    op.execute("DROP FUNCTION IF EXISTS trg_fn_document_processings_transitions();")

    # 2. Revert check constraint to 0012 definition
    op.execute(
        "ALTER TABLE document_processings "
        "DROP CONSTRAINT IF EXISTS ck_document_processings_status_fields;"
    )
    op.execute(
        """
        ALTER TABLE document_processings ADD CONSTRAINT ck_document_processings_status_fields
        CHECK (
            (status = 'pending' AND started_at IS NULL AND finished_at IS NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR
            (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL
             AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR
            (status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL
             AND result IS NOT NULL AND result != 'null'::jsonb AND error_code IS NULL) OR
            (status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
             AND error_code IS NOT NULL) OR
            (status = 'cancelled' AND finished_at IS NOT NULL)
        );
        """
    )
