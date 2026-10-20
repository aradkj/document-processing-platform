"""Initial baseline migration.

Revision ID: 0001_initial_baseline
Revises:
Create Date: 2026-10-20 00:00:00.000000+00:00

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_initial_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Initial baseline migration (Phase 1 foundation)."""
    pass


def downgrade() -> None:
    """Revert initial baseline migration."""
    pass
