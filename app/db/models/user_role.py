"""UserRole association model definition."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserRole(Base):
    """Association model linking Users to their assigned Roles."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Initialize a UserRole association."""
        super().__init__(
            user_id=user_id,
            role_id=role_id,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Safe representation showing associated IDs."""
        return f"<UserRole user_id={self.user_id} role_id={self.role_id}>"
