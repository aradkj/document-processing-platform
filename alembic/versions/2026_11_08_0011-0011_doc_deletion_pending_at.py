"""Add deletion_pending_at to documents table.

Revision ID: 0011_doc_deletion_pending_at
Revises: 0010_create_documents_table
Create Date: 2026-11-08 20:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_doc_deletion_pending_at"
down_revision: str | None = "0010_create_documents_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add deletion_pending_at timestamp column to documents table."""
    op.add_column(
        "documents",
        sa.Column(
            "deletion_pending_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove deletion_pending_at column from documents table."""
    op.drop_column("documents", "deletion_pending_at")
