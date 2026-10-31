"""User domain model definition."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.role import Role


class User(Base):
    """User account entity representing a registered platform user."""

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_email_lower", func.lower(text("email")), unique=True),
        Index("uq_users_username_lower", func.lower(text("username")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
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

    # Relationships (lazy="raise" prevents implicit sync I/O in async context)
    roles: Mapped[list["Role"]] = relationship(
        "Role",
        secondary="user_roles",
        back_populates="users",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        email: str,
        username: str,
        password_hash: str,
        is_active: bool = True,
        is_verified: bool = False,
        last_login_at: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a User instance with defaults."""
        super().__init__(
            id=id or uuid.uuid4(),
            email=email,
            username=username,
            password_hash=password_hash,
            is_active=is_active,
            is_verified=is_verified,
            last_login_at=last_login_at,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Safe representation hiding sensitive credentials."""
        return f"<User id={self.id} username={self.username} email={self.email}>"
