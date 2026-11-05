"""Password reset token and session lifecycle management service."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    generate_password_reset_token,
    get_password_hash,
    hash_password_reset_token,
    validate_password_policy,
    verify_dummy_password,
)
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from app.services.auth import get_user_by_email, revoke_all_user_refresh_tokens
from app.services.email import EmailService, get_email_service

logger = logging.getLogger(__name__)


class PasswordResetError(Exception):
    """Base domain exception for password reset operations."""

    pass


class InvalidPasswordResetTokenError(PasswordResetError):
    """Raised when token is missing, expired, consumed, invalidated, or email-mismatched."""

    pass


async def invalidate_user_reset_tokens(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Invalidate all unconsumed active password reset tokens for a user."""
    now = datetime.now(UTC)
    stmt = (
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0))


async def create_password_reset_token(
    session: AsyncSession,
    user_id: uuid.UUID,
    email: str,
) -> tuple[PasswordResetToken, str]:
    """Generate high-entropy reset token, hash it, invalidate prior tokens, and save hash."""
    settings = get_settings()
    raw_token = generate_password_reset_token()
    token_hash = hash_password_reset_token(raw_token)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)

    # Invalidate previous unconsumed tokens for this user
    await invalidate_user_reset_tokens(session, user_id)

    token_record = PasswordResetToken(
        user_id=user_id,
        email=email.strip().lower(),
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(token_record)
    await session.flush()

    return token_record, raw_token


async def request_password_reset(
    session: AsyncSession,
    email: str,
    email_service: EmailService | None = None,
) -> bool:
    """Handle a public password reset request.

    Anti-enumeration & Security guarantees:
    - Normalizes email input consistently.
    - If user does not exist or is inactive, performs dummy password hashing to match timing
      and returns generic success message without sending email or creating a token.
    - If user exists and is active, locks row with FOR UPDATE, invalidates prior tokens,
      persists a new SHA-256 token hash, commits the transaction, and delivers the reset email.
    """
    normalized_email = email.strip().lower()

    # Query for the user by normalized email
    user = await get_user_by_email(session, normalized_email)

    if user is None or not user.is_active:
        # Constant-time dummy verification to mitigate user enumeration timing attacks
        verify_dummy_password("dummy_constant_time_string")
        logger.info(
            "Password reset requested for nonexistent or inactive email: %s", normalized_email
        )
        return True

    # Lock user row to serialize concurrent reset token requests
    lock_stmt = select(User.id).where(User.id == user.id).with_for_update()
    await session.execute(lock_stmt)

    _token_record, raw_token = await create_password_reset_token(
        session=session,
        user_id=user.id,
        email=user.email,
    )
    await session.commit()

    # Deliver reset email post-commit using existing EmailService
    try:
        email_svc = email_service or get_email_service()
        await email_svc.send_password_reset_email(
            to_email=user.email,
            raw_token=raw_token,
        )
    except Exception as exc:
        logger.warning(
            "Non-fatal error delivering password reset email to %s: %s",
            user.email,
            exc,
        )

    return True


async def confirm_password_reset(
    session: AsyncSession,
    raw_token: str,
    new_password: str,
) -> User:
    """Consume a valid reset token and atomically reset user's password, invalidating all sessions.

    Atomicity & Security Guarantees:
    - Validates token format and computes SHA-256 hash.
    - Locks the token record with FOR UPDATE.
    - Validates expiration, single-use (used_at is None), invalidation (invalidated_at is None),
      and bound email snapshot match.
    - Locks user record with FOR UPDATE.
    - Validates new password policy and derives new Argon2id hash.
    - Updates user.password_hash.
    - Increments user.auth_version by 1 (invalidating every existing access JWT immediately).
    - Revokes every active refresh token family for this user in PostgreSQL.
    - Marks token as used (used_at=now).
    - Invalidates any other outstanding reset tokens for this user.
    - Commits all changes in a single atomic database transaction.
    """
    if not raw_token or not raw_token.strip():
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    validate_password_policy(new_password)
    token_hash = hash_password_reset_token(raw_token.strip())
    now = datetime.now(UTC)

    # 1. Fetch and lock the token record
    token_stmt = (
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash)
        .with_for_update()
    )
    token_result = await session.execute(token_stmt)
    token = token_result.scalar_one_or_none()

    if token is None:
        logger.warning("Password reset failed: token hash not found")
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    if token.used_at is not None:
        logger.warning("Password reset failed: token already consumed (used_at=%s)", token.used_at)
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    if token.invalidated_at is not None:
        logger.warning(
            "Password reset failed: token invalidated (invalidated_at=%s)", token.invalidated_at
        )
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    if token.expires_at <= now:
        logger.warning("Password reset failed: token expired (expires_at=%s)", token.expires_at)
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    # 2. Fetch and lock user record
    user_stmt = select(User).where(User.id == token.user_id).with_for_update()
    user_result = await session.execute(user_stmt)
    user = user_result.scalar_one_or_none()

    if user is None or not user.is_active:
        logger.warning(
            "Password reset failed: user not found or inactive (user_id=%s)", token.user_id
        )
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    # 3. Check bound email snapshot match
    if user.email.lower() != token.email.lower():
        logger.warning(
            "Password reset failed: email snapshot mismatch (token.email=%s, user.email=%s)",
            token.email,
            user.email,
        )
        raise InvalidPasswordResetTokenError("Invalid or expired password reset token.")

    # 4. Hash new password with Argon2id
    new_password_hash = get_password_hash(new_password)

    # 5. Atomic state update
    # Invalidate all existing active reset tokens for this user
    await invalidate_user_reset_tokens(session, user.id)

    # Revoke every active refresh token family for this user
    await revoke_all_user_refresh_tokens(session, user.id, commit=False)

    # Increment authentication version to invalidate existing access tokens live
    user.password_hash = new_password_hash
    user.auth_version += 1

    # Mark this token as consumed
    token.used_at = now

    # Commit the single atomic transaction
    await session.commit()
    logger.info("Password successfully reset and sessions revoked for user_id=%s", user.id)

    return user
