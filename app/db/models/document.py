"""Document domain model definition."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


class Document(Base):
    """Document entity storing file metadata and ownership information in PostgreSQL.

    Binary file contents are persisted separately via StorageBackend using `storage_key`.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        CheckConstraint("storage_key ~ '^[0-9a-f]{32}$'", name="storage_key_hex32"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex64"),
        Index("ix_documents_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    storage_key: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    content_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deletion_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # Relationships (lazy="raise" prevents implicit sync I/O in async context)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="documents",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        user_id: uuid.UUID,
        storage_key: str,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        deletion_pending_at: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a Document entity record."""
        super().__init__(
            id=id or uuid.uuid4(),
            user_id=user_id,
            storage_key=storage_key,
            original_filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            **kwargs,
        )
        if created_at is not None:
            self.created_at = created_at
        if updated_at is not None:
            self.updated_at = updated_at
        if deletion_pending_at is not None:
            self.deletion_pending_at = deletion_pending_at

    def __repr__(self) -> str:
        """Safe representation showing document identity, owner, and storage key."""
        return (
            f"<Document id={self.id} user_id={self.user_id} "
            f"storage_key={self.storage_key} filename={self.original_filename!r}>"
        )
