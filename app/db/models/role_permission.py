"""RolePermission association model definition."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RolePermission(Base):
    """Association model linking Roles to their granted Permissions."""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
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
        role_id: uuid.UUID,
        permission_id: uuid.UUID,
        **kwargs: Any,
    ) -> None:
        """Initialize a RolePermission association."""
        super().__init__(
            role_id=role_id,
            permission_id=permission_id,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Safe representation showing associated IDs."""
        return f"<RolePermission role_id={self.role_id} permission_id={self.permission_id}>"
