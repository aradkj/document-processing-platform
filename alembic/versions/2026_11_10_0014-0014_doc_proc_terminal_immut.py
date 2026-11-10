"""Enforce strict terminal record immutability via trigger.

Revision ID: 0014_doc_proc_terminal_immut
Revises: 0013_doc_proc_transitions
Create Date: 2026-11-10 19:35:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_doc_proc_terminal_immut"
down_revision: str | None = "0013_doc_proc_transitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Upgrade trigger function: unconditionally rejects ANY UPDATE on terminal records
UPGRADE_TRIGGER_FN_SQL = """
CREATE OR REPLACE FUNCTION trg_fn_document_processings_transitions()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- Prevent ANY modification to records in an immutable terminal state
        IF OLD.status IN ('succeeded', 'failed', 'cancelled') THEN
            RAISE EXCEPTION
                'Cannot update document_processing %: '
                'record is in terminal state % and is immutable',
                OLD.id, OLD.status
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

# Downgrade trigger function: restores 0013 behavior (status-change-only check)
DOWNGRADE_TRIGGER_FN_SQL = """
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
    """Replace trigger function with strict terminal immutability implementation."""
    op.execute(UPGRADE_TRIGGER_FN_SQL)


def downgrade() -> None:
    """Revert trigger function to 0013 status-change-only implementation."""
    op.execute(DOWNGRADE_TRIGGER_FN_SQL)
