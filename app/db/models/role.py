"""Role domain model definition."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.permission import Permission
    from app.db.models.user import User


class Role(Base):
    """Role entity representing a named authorization bundle."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
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
    users: Mapped[list["User"]] = relationship(
        "User",
        secondary="user_roles",
        back_populates="roles",
        lazy="raise",
    )
    permissions: Mapped[list["Permission"]] = relationship(
        "Permission",
        secondary="role_permissions",
        back_populates="roles",
        lazy="raise",
    )

    def __init__(
        self,
        *,
        id: uuid.UUID | None = None,
        name: str,
        description: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a Role instance with defaults."""
        super().__init__(
            id=id or uuid.uuid4(),
            name=name,
            description=description,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Safe representation hiding internal details."""
        return f"<Role id={self.id} name={self.name}>"
