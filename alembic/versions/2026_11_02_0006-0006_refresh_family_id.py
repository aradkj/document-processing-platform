"""Add family_id to refresh_tokens table.

Revision ID: 0006_refresh_family_id
Revises: 0005_create_refresh_tokens_table
Create Date: 2026-11-02 22:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_refresh_family_id"
down_revision: str | None = "0005_create_refresh_tokens_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add family_id column with index to refresh_tokens table."""
    op.add_column(
        "refresh_tokens",
        sa.Column("family_id", sa.Uuid(), nullable=True),
    )
    op.execute(sa.text("UPDATE refresh_tokens SET family_id = id WHERE family_id IS NULL"))
    op.alter_column(
        "refresh_tokens",
        "family_id",
        nullable=False,
    )
    op.create_index(
        op.f("ix_refresh_tokens_family_id"),
        "refresh_tokens",
        ["family_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove family_id index and column from refresh_tokens table."""
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    op.drop_column("refresh_tokens", "family_id")
