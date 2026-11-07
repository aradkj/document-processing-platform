"""Create documents table.

Revision ID: 0010_create_documents_table
Revises: 0009_password_reset_tokens
Create Date: 2026-11-06 20:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_create_documents_table"
down_revision: str | None = "0009_password_reset_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create documents table with constraints and indexes."""
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(length=32), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f("ck_documents_size_bytes_non_negative"),
        ),
        sa.CheckConstraint(
            "storage_key ~ '^[0-9a-f]{32}$'",
            name=op.f("ck_documents_storage_key_hex32"),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_documents_sha256_hex64"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_documents_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_documents_storage_key")),
    )
    op.create_index(
        op.f("ix_documents_user_id_created_at"),
        "documents",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop documents table and associated indexes/constraints."""
    op.drop_index(op.f("ix_documents_user_id_created_at"), table_name="documents")
    op.drop_table("documents")

