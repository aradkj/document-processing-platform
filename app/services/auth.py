"""Authentication and user provisioning application services."""

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.db.models.role import Role
from app.db.models.user import User
from app.db.models.user_role import UserRole
from app.schemas.auth import UserRegisterRequest

logger = logging.getLogger(__name__)


class DuplicateEmailError(Exception):
    """Raised when attempting to register with an email that already exists."""

    pass


class DuplicateUsernameError(Exception):
    """Raised when attempting to register with a username that already exists."""

    pass


class DefaultRoleNotFoundError(RuntimeError):
    """Raised when the mandatory default 'user' role is missing from the database."""

    pass


async def register_user(session: AsyncSession, payload: UserRegisterRequest) -> User:
    """Register a new user, hash password, assign default role, and commit transactionally.

    Flow:
    1. Pre-check for duplicate case-insensitive email or username.
    2. Hash password securely using Argon2id.
    3. Retrieve the foundational 'user' role.
    4. Create User entity and UserRole association in one atomic transaction.
    5. Safely handle concurrent race conditions via database-level unique constraints.
    """
    normalized_email = payload.email.strip().lower()
    normalized_username = payload.username.strip()

    # 1. Pre-flight duplicate check (case-insensitive)
    existing_email = await session.execute(
        select(User.id).where(func.lower(User.email) == normalized_email)
    )
    if existing_email.scalar_one_or_none() is not None:
        raise DuplicateEmailError("An account with this email already exists.")

    existing_username = await session.execute(
        select(User.id).where(func.lower(User.username) == normalized_username.lower())
    )
    if existing_username.scalar_one_or_none() is not None:
        raise DuplicateUsernameError("An account with this username already exists.")

    # 2. Retrieve foundational default 'user' role
    role_result = await session.execute(select(Role).where(Role.name == "user"))
    default_role = role_result.scalar_one_or_none()
    if default_role is None:
        logger.error("Default 'user' role is missing from database during registration.")
        raise DefaultRoleNotFoundError("Required default 'user' role is not configured.")

    # 3. Hash plaintext password using Argon2id
    password_hash = get_password_hash(payload.password)

    # 4. Construct user and association entities
    new_user = User(
        email=normalized_email,
        username=normalized_username,
        password_hash=password_hash,
        is_active=True,
        is_verified=False,
    )
    session.add(new_user)
    await session.flush()

    user_role_assignment = UserRole(
        user_id=new_user.id,
        role_id=default_role.id,
    )
    session.add(user_role_assignment)

    # 5. Atomic commit with race-condition translation
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        err_text = str(exc.orig).lower() if hasattr(exc, "orig") else str(exc).lower()
        logger.warning(
            "Database unique constraint violation during registration: %s",
            type(exc).__name__,
        )
        if "email" in err_text:
            raise DuplicateEmailError("An account with this email already exists.") from exc
        elif "username" in err_text:
            raise DuplicateUsernameError("An account with this username already exists.") from exc
        raise

    logger.info("Successfully registered new user: %s (id=%s)", new_user.username, new_user.id)
    return new_user
