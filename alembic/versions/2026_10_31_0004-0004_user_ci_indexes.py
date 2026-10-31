"""Add case-insensitive unique indexes for email and username.

Revision ID: 0004_user_ci_indexes
Revises: 0003_create_roles_permissions
Create Date: 2026-10-31 23:05:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_user_ci_indexes"
down_revision: str | None = "0003_create_roles_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create unique functional indexes on lower(email) and lower(username)."""
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )
    op.create_index(
        "uq_users_username_lower",
        "users",
        [sa.text("lower(username)")],
        unique=True,
    )


def downgrade() -> None:
    """Drop case-insensitive functional indexes."""
    op.drop_index("uq_users_username_lower", table_name="users")
    op.drop_index("uq_users_email_lower", table_name="users")
