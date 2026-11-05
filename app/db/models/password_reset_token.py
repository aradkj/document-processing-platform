"""PasswordResetToken domain model definition for password reset workflows."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


class PasswordResetToken(Base):
    """Persistent password reset token entity storing hashed tokens and lifecycle state."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index(
            "ix_password_reset_tokens_user_state",
            "user_id",
            "used_at",
            "invalidated_at",
        ),
        Index("ix_password_reset_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    # Relationships (lazy="raise" prevents implicit sync I/O in async context)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="password_reset_tokens",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        user_id: uuid.UUID,
        email: str,
        token_hash: str,
        expires_at: datetime,
        created_at: datetime | None = None,
        used_at: datetime | None = None,
        invalidated_at: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize PasswordResetToken entity."""
        super().__init__(
            id=id or uuid.uuid4(),
            user_id=user_id,
            email=email,
            token_hash=token_hash,
            expires_at=expires_at,
            created_at=created_at or datetime.now(UTC),
            used_at=used_at,
            invalidated_at=invalidated_at,
            **kwargs,
        )

    @property
    def is_expired(self) -> bool:
        """Evaluate if the reset token has passed its expiration timestamp."""
        now = datetime.now(UTC)
        expiry = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=UTC)
        return now >= expiry

    @property
    def is_used(self) -> bool:
        """Check if the token has been consumed."""
        return self.used_at is not None

    @property
    def is_invalidated(self) -> bool:
        """Check if the token has been explicitly invalidated."""
        return self.invalidated_at is not None

    @property
    def is_active(self) -> bool:
        """Check if token is active (not used, not invalidated, and not expired)."""
        return self.used_at is None and self.invalidated_at is None and not self.is_expired

    def __repr__(self) -> str:
        """Safe representation hiding token hash."""
        return (
            f"<PasswordResetToken id={self.id} user_id={self.user_id} "
            f"email={self.email} active={self.is_active}>"
        )
