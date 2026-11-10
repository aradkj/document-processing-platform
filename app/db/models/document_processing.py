"""Document processing domain model definition."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.document import Document


class ProcessingLifecycleStatus(StrEnum):
    """Lifecycle states for a document processing execution."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PROCESSING_STATUSES: frozenset[ProcessingLifecycleStatus] = frozenset(
    {
        ProcessingLifecycleStatus.SUCCEEDED,
        ProcessingLifecycleStatus.FAILED,
        ProcessingLifecycleStatus.CANCELLED,
    }
)

ALLOWED_PROCESSING_TRANSITIONS: dict[
    ProcessingLifecycleStatus, frozenset[ProcessingLifecycleStatus]
] = {
    ProcessingLifecycleStatus.PENDING: frozenset(
        {
            ProcessingLifecycleStatus.RUNNING,
            ProcessingLifecycleStatus.CANCELLED,
        }
    ),
    ProcessingLifecycleStatus.RUNNING: frozenset(
        {
            ProcessingLifecycleStatus.SUCCEEDED,
            ProcessingLifecycleStatus.FAILED,
            ProcessingLifecycleStatus.CANCELLED,
        }
    ),
    ProcessingLifecycleStatus.SUCCEEDED: frozenset(),
    ProcessingLifecycleStatus.FAILED: frozenset(),
    ProcessingLifecycleStatus.CANCELLED: frozenset(),
}


class DocumentProcessing(Base):
    """Entity representing a single processing execution attempt for a document."""

    __tablename__ = "document_processings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_document_processings_status",
        ),
        CheckConstraint(
            "input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_processings_input_sha256",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="ck_document_processings_timestamps",
        ),
        CheckConstraint(
            "("
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL) OR "
            "(status = 'succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND result IS NOT NULL AND result != 'null'::jsonb AND error_code IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NOT NULL) OR "
            "(status = 'cancelled' AND finished_at IS NOT NULL "
            "AND (result IS NULL OR result = 'null'::jsonb) AND error_code IS NULL)"
            ")",
            name="ck_document_processings_status_fields",
        ),
        Index("ix_document_processings_document_id_created_at", "document_id", "created_at"),
        Index("ix_document_processings_status_created_at", "status", "created_at"),
        Index("ix_document_processings_document_id_status", "document_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    processor_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    processor_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ProcessingLifecycleStatus.PENDING.value,
        server_default=ProcessingLifecycleStatus.PENDING.value,
    )
    input_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    result_schema_version: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        default=None,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        default=None,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship to Document (lazy="raise" prevents implicit sync I/O in async context)
    document: Mapped[Document] = relationship(
        "Document",
        back_populates="processing_runs",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        document_id: uuid.UUID,
        processor_name: str,
        processor_version: str,
        input_sha256: str,
        status: str = ProcessingLifecycleStatus.PENDING.value,
        result_schema_version: str | None = None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        created_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        updated_at: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a DocumentProcessing entity record."""
        now = datetime.now(UTC)
        super().__init__(**kwargs)
        self.id = id if id is not None else uuid.uuid4()
        self.document_id = document_id
        self.processor_name = processor_name
        self.processor_version = processor_version
        self.input_sha256 = input_sha256
        self.status = status
        self.result_schema_version = result_schema_version
        self.result = result
        self.error_code = error_code
        self.error_message = error_message
        self.created_at = created_at or now
        self.started_at = started_at
        self.finished_at = finished_at
        self.updated_at = updated_at or now

    @property
    def is_terminal(self) -> bool:
        """Whether this processing execution is in a final, immutable state."""
        return self.status in TERMINAL_PROCESSING_STATUSES

    @property
    def is_active(self) -> bool:
        """Whether this processing execution is currently pending or running."""
        return self.status in (
            ProcessingLifecycleStatus.PENDING,
            ProcessingLifecycleStatus.RUNNING,
        )

    @property
    def duration_seconds(self) -> float | None:
        """Total execution duration in seconds if started and finished, else None."""
        if self.started_at is not None and self.finished_at is not None:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def __repr__(self) -> str:
        return (
            f"<DocumentProcessing id={self.id} doc_id={self.document_id} "
            f"proc={self.processor_name}:{self.processor_version} status={self.status}>"
        )
