"""Email verification service handling token lifecycle, state validation, and consumption."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import generate_email_verification_token, hash_email_verification_token
from app.db.models.email_verification_token import EmailVerificationToken
from app.db.models.user import User
from app.services.email import EmailService, get_email_service

logger = logging.getLogger(__name__)


class EmailVerificationError(Exception):
    """Base exception for email verification domain errors."""

    pass


class InvalidVerificationTokenError(EmailVerificationError):
    """Raised when a verification token is invalid, expired, used, or superseded."""

    pass


class UserAlreadyVerifiedError(EmailVerificationError):
    """Raised when an operation is attempted on an account whose email is already verified."""

    pass


async def invalidate_user_verification_tokens(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Invalidate all active, unconsumed verification tokens for a given user.

    Returns the count of tokens marked as superseded/invalidated.
    """
    stmt = (
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=datetime.now(UTC))
    )
    result = await session.execute(stmt)
    count = int(getattr(result, "rowcount", 0))
    logger.debug("Invalidated %d active verification tokens for user_id=%s", count, user_id)
    return count


async def create_email_verification_token(
    session: AsyncSession,
    user_id: uuid.UUID,
    email: str,
    expires_in_minutes: int | None = None,
) -> tuple[EmailVerificationToken, str]:
    """Generate and persist a single-use verification token bound to user and email.

    Acquires an exclusive row lock (FOR UPDATE) on the associated User row to
    strictly serialize token issuance and invalidation per account under concurrent requests.
    Automatically invalidates any preexisting active tokens for the user to ensure
    at most one valid verification token exists at any time.

    Returns a tuple of (persisted_token_record, raw_token_string).
    """
    settings = get_settings()
    expiry_minutes = (
        expires_in_minutes
        if expires_in_minutes is not None
        else settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES
    )

    # 1. Acquire row lock on parent User to serialize token operations for this user
    user_lock_stmt = select(User.id).where(User.id == user_id).with_for_update()
    await session.execute(user_lock_stmt)

    # 2. Invalidate previous active tokens for this user
    await invalidate_user_verification_tokens(session, user_id)

    raw_token = generate_email_verification_token()
    token_hash = hash_email_verification_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(minutes=expiry_minutes)

    token_record = EmailVerificationToken(
        user_id=user_id,
        email=email.strip().lower(),
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(token_record)
    await session.flush()

    logger.info(
        "Created verification token record id=%s for user_id=%s (expires in %dm)",
        token_record.id,
        user_id,
        expiry_minutes,
    )
    return token_record, raw_token


async def verify_email_token(
    session: AsyncSession,
    raw_token: str,
) -> User:
    """Consume a verification token, mark token used, and set user.is_verified=True.

    Uses PostgreSQL row-level locking (with_for_update) to guarantee atomic single-use
    consumption under concurrent verification attempts.
    """
    if not raw_token or not raw_token.strip():
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    token_hash = hash_email_verification_token(raw_token)

    # 1. Lock and retrieve token record
    stmt = (
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_hash)
        .with_for_update()
    )
    result = await session.execute(stmt)
    token_record = result.scalar_one_or_none()

    if token_record is None:
        logger.warning("Email verification rejected: token hash not found in database")
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 2. Check usage state
    if token_record.used_at is not None:
        logger.warning(
            "Email verification rejected: token id=%s already consumed at %s",
            token_record.id,
            token_record.used_at,
        )
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 3. Check invalidation state
    if token_record.invalidated_at is not None:
        logger.warning(
            "Email verification rejected: token id=%s was superseded/invalidated at %s",
            token_record.id,
            token_record.invalidated_at,
        )
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 4. Check expiration
    if token_record.is_expired:
        logger.warning(
            "Email verification rejected: token id=%s expired at %s",
            token_record.id,
            token_record.expires_at,
        )
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 5. Retrieve associated user
    user = await session.get(User, token_record.user_id)
    if user is None:
        logger.warning(
            "Email verification rejected: associated user_id=%s does not exist",
            token_record.user_id,
        )
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    if not user.is_active:
        logger.warning("Email verification rejected: user_id=%s is deactivated", user.id)
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 6. Verify account current email matches token bound email
    if user.email.lower() != token_record.email.lower():
        logger.warning(
            "Email verification rejected: user email (%s) does not match token bound email (%s)",
            user.email,
            token_record.email,
        )
        raise InvalidVerificationTokenError("Invalid or expired verification token.")

    # 7. Consume token and set user verified atomically
    now = datetime.now(UTC)
    token_record.used_at = now
    user.is_verified = True

    await session.commit()
    await session.refresh(user)

    logger.info("Successfully verified email for user %s (id=%s)", user.username, user.id)
    return user


async def resend_verification_email(
    session: AsyncSession,
    user: User,
    email_service: EmailService | None = None,
) -> bool:
    """Generate a new verification token and dispatch email for an unverified account.

    Locks the User row to serialize concurrent requests and checks fresh verification state.
    If the user is already verified, skips token generation and email dispatch safely.
    """
    # 1. Lock and re-read the User row to guarantee fresh state and serialize resends
    stmt = select(User).where(User.id == user.id).with_for_update()
    result = await session.execute(stmt)
    locked_user = result.scalar_one_or_none()

    if locked_user is None:
        logger.warning("Resend verification requested for non-existent user_id=%s", user.id)
        return False

    if locked_user.is_verified:
        logger.info("Resend verification requested for already-verified user_id=%s", locked_user.id)
        return True

    _token_record, raw_token = await create_email_verification_token(
        session=session,
        user_id=locked_user.id,
        email=locked_user.email,
    )
    await session.commit()

    service = email_service or get_email_service()
    delivered = await service.send_verification_email(
        to_email=locked_user.email,
        raw_token=raw_token,
    )
    return delivered
