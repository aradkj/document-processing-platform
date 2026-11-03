"""Optimize email_verification_tokens indexes.

Revision ID: 0008_optimize_email_token_idx
Revises: 0007_email_verify_tokens
Create Date: 2026-11-03 22:30:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_optimize_email_token_idx"
down_revision: str | None = "0007_email_verify_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Optimize indexes by adding composite user_state index and removing redundant indexes."""
    # 1. Create composite user-state index: covers (user_id, used_at, invalidated_at)
    # Since user_id is the leading column, it also handles foreign key lookups
    # and queries on user_id alone efficiently.
    op.create_index(
        op.f("ix_email_verification_tokens_user_state"),
        "email_verification_tokens",
        ["user_id", "used_at", "invalidated_at"],
        unique=False,
    )

    # 2. Drop redundant standalone index on user_id (covered by composite user_state leading column)
    op.drop_index(
        op.f("ix_email_verification_tokens_user_id"),
        table_name="email_verification_tokens",
    )

    # 3. Drop unused standalone index on created_at
    op.drop_index(
        op.f("ix_email_verification_tokens_created_at"),
        table_name="email_verification_tokens",
    )


def downgrade() -> None:
    """Recreate standalone user_id and created_at indexes and drop composite user_state index."""
    op.create_index(
        op.f("ix_email_verification_tokens_created_at"),
        "email_verification_tokens",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_email_verification_tokens_user_id"),
        "email_verification_tokens",
        ["user_id"],
        unique=False,
    )
    op.drop_index(
        op.f("ix_email_verification_tokens_user_state"),
        table_name="email_verification_tokens",
    )
