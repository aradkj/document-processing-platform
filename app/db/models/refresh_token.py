"""RefreshToken domain model definition for persistent session revocation and rotation."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


class RefreshToken(Base):
    """Persistent refresh token session entity tracking tokens, family lineage, and rotation."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        Index("ix_refresh_tokens_revoked_at", "revoked_at"),
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
    jti: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )
    family_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
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
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
    replaced_by_jti: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default=None,
    )

    # Relationships (lazy="raise" prevents implicit sync I/O in async context)
    user: Mapped["User"] = relationship(
        "User",
        back_populates="refresh_tokens",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        user_id: uuid.UUID,
        jti: str,
        family_id: uuid.UUID | None = None,
        expires_at: datetime,
        revoked_at: datetime | None = None,
        replaced_by_jti: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a RefreshToken session record."""
        super().__init__(
            id=id or uuid.uuid4(),
            user_id=user_id,
            jti=jti,
            family_id=family_id or uuid.uuid4(),
            expires_at=expires_at,
            revoked_at=revoked_at,
            replaced_by_jti=replaced_by_jti,
            **kwargs,
        )

    @property
    def is_revoked(self) -> bool:
        """Return True if session has been explicitly revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Return True if session expiration timestamp has passed in UTC."""
        now = datetime.now(UTC)
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=UTC)
        return now >= exp

    @property
    def is_replaced(self) -> bool:
        """Return True if session has been rotated/replaced by a newer token."""
        return self.replaced_by_jti is not None

    @property
    def is_active(self) -> bool:
        """Return True if session is neither revoked, expired, nor replaced."""
        return not self.is_revoked and not self.is_expired and not self.is_replaced

    def revoke(self, replaced_by: str | None = None) -> None:
        """Mark session as revoked at current UTC timestamp."""
        if self.revoked_at is None:
            self.revoked_at = datetime.now(UTC)
        if replaced_by is not None:
            self.replaced_by_jti = replaced_by

    def mark_replaced(self, new_jti: str) -> None:
        """Mark session as consumed/rotated by a new token JTI and set revoked_at."""
        self.replaced_by_jti = new_jti
        if self.revoked_at is None:
            self.revoked_at = datetime.now(UTC)

    def __repr__(self) -> str:
        """Safe representation hiding raw secrets."""
        return (
            f"<RefreshToken id={self.id} user_id={self.user_id} "
            f"family_id={self.family_id} jti={self.jti} "
            f"revoked={self.is_revoked} replaced={self.is_replaced}>"
        )
