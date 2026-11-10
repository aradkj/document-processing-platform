"""Create document_processings table.

Revision ID: 0012_doc_processing_lifecycle
Revises: 0011_doc_deletion_pending_at
Create Date: 2026-11-09 20:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_doc_processing_lifecycle"
down_revision: str | None = "0011_doc_deletion_pending_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create document_processings table with lifecycle constraints and indexes."""
    op.create_table(
        "document_processings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processor_name", sa.String(length=64), nullable=False),
        sa.Column("processor_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("result_schema_version", sa.String(length=32), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_document_processings_status"),
        ),
        sa.CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_document_processings_input_sha256"),
        ),
        sa.CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_document_processings_timestamps"),
        ),
        sa.CheckConstraint(
            "("
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR "
            "(status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND result IS NOT NULL AND result != 'null'::jsonb AND error_code IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL) OR "
            "(status = 'cancelled' AND finished_at IS NOT NULL)"
            ")",
            name=op.f("ck_document_processings_status_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_processings_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_processings")),
    )
    op.create_index(
        op.f("ix_document_processings_document_id_created_at"),
        "document_processings",
        ["document_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_processings_status_created_at"),
        "document_processings",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_processings_document_id_status"),
        "document_processings",
        ["document_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    """Drop document_processings table and associated indexes."""
    op.drop_index(
        op.f("ix_document_processings_document_id_status"),
        table_name="document_processings",
    )
    op.drop_index(
        op.f("ix_document_processings_status_created_at"),
        table_name="document_processings",
    )
    op.drop_index(
        op.f("ix_document_processings_document_id_created_at"),
        table_name="document_processings",
    )
    op.drop_table("document_processings")
