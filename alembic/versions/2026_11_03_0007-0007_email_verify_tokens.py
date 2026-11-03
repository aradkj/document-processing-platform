"""Create email_verification_tokens table.

Revision ID: 0007_email_verify_tokens
Revises: 0006_refresh_family_id
Create Date: 2026-11-03 21:30:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_email_verify_tokens"
down_revision: str | None = "0006_refresh_family_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create email_verification_tokens table with constraints and indexes."""
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_verification_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_tokens")),
    )
    op.create_index(
        op.f("ix_email_verification_tokens_user_id"),
        "email_verification_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_verification_tokens_token_hash"),
        "email_verification_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_email_verification_tokens_expires_at"),
        "email_verification_tokens",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_verification_tokens_created_at"),
        "email_verification_tokens",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop email_verification_tokens table and associated indexes."""
    op.drop_index(
        op.f("ix_email_verification_tokens_created_at"),
        table_name="email_verification_tokens",
    )
    op.drop_index(
        op.f("ix_email_verification_tokens_expires_at"),
        table_name="email_verification_tokens",
    )
    op.drop_index(
        op.f("ix_email_verification_tokens_token_hash"),
        table_name="email_verification_tokens",
    )
    op.drop_index(
        op.f("ix_email_verification_tokens_user_id"),
        table_name="email_verification_tokens",
    )
    op.drop_table("email_verification_tokens")
