"""Authentication and user provisioning application services."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import (
    TokenSecurityError,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_password_hash,
    verify_dummy_password,
    verify_password,
)
from app.db.models.refresh_token import RefreshToken
from app.db.models.role import Role
from app.db.models.user import User
from app.db.models.user_role import UserRole
from app.schemas.auth import (
    UserLoginRequest,
    UserProfileUpdateRequest,
    UserRegisterRequest,
)
from app.services.email import EmailService, get_email_service
from app.services.email_verification import create_email_verification_token

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


class InvalidCredentialsError(Exception):
    """Raised when authentication credentials (email/password) are incorrect."""

    pass


class InactiveUserError(Exception):
    """Raised when an inactive user attempts to authenticate or access resources."""

    pass


class InvalidRefreshTokenError(Exception):
    """Raised when a refresh token is invalid, expired, or malformed."""

    pass


class UserNotFoundError(Exception):
    """Raised when a referenced user record cannot be found."""

    pass


async def register_user(
    session: AsyncSession,
    payload: UserRegisterRequest,
    email_service: EmailService | None = None,
) -> User:
    """Register a new user, hash password, assign default role, and commit transactionally.

    Flow:
    1. Pre-check for duplicate case-insensitive email or username.
    2. Hash password securely using Argon2id.
    3. Retrieve the foundational 'user' role.
    4. Create User entity, default role association, and initial verification token.
    5. Commit atomically, translating database unique constraints to domain errors.
    6. Deliver verification email post-commit (safe failure policy).
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

    # Generate initial single-use verification token (hashed before storage)
    _token_record, raw_token = await create_email_verification_token(
        session=session,
        user_id=new_user.id,
        email=new_user.email,
    )

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

    # 6. Post-commit verification email delivery (safe failure policy)
    try:
        email_svc = email_service or get_email_service()
        await email_svc.send_verification_email(
            to_email=new_user.email,
            raw_token=raw_token,
        )
    except Exception as exc:
        logger.warning(
            "Non-fatal error delivering verification email during registration for %s: %s",
            new_user.email,
            exc,
        )

    return new_user


async def authenticate_user(
    session: AsyncSession, payload: UserLoginRequest
) -> tuple[User, str, str, int]:
    """Authenticate an existing user via email and password, returning access and refresh tokens.

    Security & Behavior:
    - Normalizes email to lowercase and trims whitespace.
    - Mitigates user enumeration timing attacks via constant-time dummy password hashing.
    - Validates Argon2id password hash.
    - Verifies user account is active (`is_active == True`).
    - Updates `last_login_at` timestamp in UTC upon success.
    - Issues cryptographically signed short-lived JWT access token.
    - Issues cryptographically signed long-lived JWT refresh token.
    """
    normalized_email = payload.email.strip().lower()

    result = await session.execute(select(User).where(func.lower(User.email) == normalized_email))
    user = result.scalar_one_or_none()

    if user is None:
        # Constant-time dummy verification to protect against email enumeration
        verify_dummy_password(payload.password)
        logger.info("Authentication rejected: user not found")
        raise InvalidCredentialsError("Invalid email or password.")

    if not verify_password(payload.password, user.password_hash):
        logger.info("Authentication rejected: password mismatch for user_id=%s", user.id)
        raise InvalidCredentialsError("Invalid email or password.")

    if not user.is_active:
        logger.warning("Authentication rejected: inactive account for user_id=%s", user.id)
        raise InactiveUserError("User account is inactive.")

    # Update last_login_at timestamp on successful authentication
    user.last_login_at = datetime.now(UTC)

    settings = get_settings()
    refresh_jti = uuid.uuid4().hex
    family_id = uuid.uuid4()
    now = datetime.now(UTC)
    refresh_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    refresh_expires_at = now + refresh_delta

    access_token = create_access_token(subject=str(user.id))
    refresh_token = create_refresh_token(
        subject=str(user.id),
        expires_delta=refresh_delta,
        jti=refresh_jti,
    )
    expires_in = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60

    # Persist the refresh token session record transactionally with new family_id
    session_record = RefreshToken(
        user_id=user.id,
        jti=refresh_jti,
        family_id=family_id,
        expires_at=refresh_expires_at,
    )
    session.add(session_record)
    await session.commit()

    logger.info(
        "User authenticated successfully: user_id=%s (family_id=%s, jti=%s)",
        user.id,
        family_id,
        refresh_jti,
    )
    return user, access_token, refresh_token, expires_in


async def get_refresh_token_session_by_jti(session: AsyncSession, jti: str) -> RefreshToken | None:
    """Retrieve a persistent RefreshToken session by JTI identifier."""
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    return result.scalar_one_or_none()


async def revoke_token_family(session: AsyncSession, family_id: uuid.UUID) -> int:
    """Atomically revoke all active refresh tokens belonging to a token family."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()
    count = int(getattr(result, "rowcount", 0))
    logger.warning("Token family revoked: family_id=%s count=%s", family_id, count)
    return count


async def refresh_access_token(
    session: AsyncSession, refresh_token: str
) -> tuple[User, str, str, int]:
    """Validate a refresh token, consume it, and rotate to a new access/refresh token pair.

    Security & Behavior:
    - Cryptographically decodes & validates refresh token (type=='refresh', alg pinning, exp, jti).
    - Extracts JTI and subject user_id.
    - Locks row using SELECT ... FOR UPDATE to prevent concurrent double-spend / split-brain.
    - Detects refresh token reuse: if token was already replaced, atomically revokes the
      entire token family and returns InvalidRefreshTokenError.
    - Verifies session is not revoked and not expired.
    - Resolves live User in PostgreSQL (rejects deleted and inactive users).
    - Rotates token: generates new refresh token JTI, marks old token as replaced,
      persists new refresh token in the same family, generates new access token.
    - Commits transaction atomically.
    """
    try:
        payload = decode_refresh_token(refresh_token)
    except TokenSecurityError as exc:
        logger.info("Token refresh rejected: %s", exc)
        raise InvalidRefreshTokenError("Invalid refresh token.") from exc

    subject = payload.get("sub")
    jti = payload.get("jti")
    if not subject or not jti:
        raise InvalidRefreshTokenError("Invalid refresh token.")

    try:
        user_id = uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        raise InvalidRefreshTokenError("Invalid refresh token.") from exc

    # Row lock with FOR UPDATE to prevent race conditions during concurrent refresh requests
    stmt = select(RefreshToken).where(RefreshToken.jti == str(jti)).with_for_update()
    result = await session.execute(stmt)
    token_session = result.scalar_one_or_none()

    if token_session is None:
        logger.info("Token refresh rejected: session record not found for jti=%s", jti)
        raise InvalidRefreshTokenError("Invalid refresh token.")

    # REUSE DETECTION: If token was already replaced, invalidate entire token family
    if token_session.is_replaced:
        logger.warning(
            "Security event - refresh token reuse detected: jti=%s user_id=%s family_id=%s",
            jti,
            token_session.user_id,
            token_session.family_id,
        )
        await revoke_token_family(session, token_session.family_id)
        raise InvalidRefreshTokenError("Invalid refresh token.")

    if token_session.is_revoked:
        logger.info("Token refresh rejected: session jti=%s is revoked", jti)
        raise InvalidRefreshTokenError("Invalid refresh token.")

    if token_session.is_expired:
        logger.info("Token refresh rejected: session jti=%s is expired", jti)
        raise InvalidRefreshTokenError("Invalid refresh token.")

    if token_session.user_id != user_id:
        logger.warning("Token refresh rejected: session user_id mismatch")
        raise InvalidRefreshTokenError("Invalid refresh token.")

    user = await get_user_by_id(session, user_id)
    if user is None:
        logger.info("Token refresh rejected: user_id=%s not found", user_id)
        raise InvalidRefreshTokenError("Invalid refresh token.")

    if not user.is_active:
        logger.warning("Token refresh rejected: user_id=%s is inactive", user_id)
        raise InactiveUserError("User account is inactive.")

    settings = get_settings()
    now = datetime.now(UTC)
    refresh_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    new_expires_at = now + refresh_delta
    new_refresh_jti = uuid.uuid4().hex

    # 1. Create new short-lived access token
    new_access_token = create_access_token(subject=str(user.id))
    # 2. Create new rotated refresh token with the same family_id
    new_refresh_token = create_refresh_token(
        subject=str(user.id),
        expires_delta=refresh_delta,
        jti=new_refresh_jti,
    )
    # 3. Mark current token as consumed/replaced
    token_session.mark_replaced(new_refresh_jti)

    # 4. Persist new session record in PostgreSQL within the same family
    new_session_record = RefreshToken(
        user_id=user.id,
        jti=new_refresh_jti,
        family_id=token_session.family_id,
        expires_at=new_expires_at,
    )
    session.add(new_session_record)

    # 5. Atomic commit
    await session.commit()

    expires_in = settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    logger.info(
        "Token rotated successfully: user_id=%s family_id=%s old_jti=%s new_jti=%s",
        user.id,
        token_session.family_id,
        jti,
        new_refresh_jti,
    )
    return user, new_access_token, new_refresh_token, expires_in


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> bool:
    """Cryptographically verify a refresh token and revoke its persistent token family.

    Idempotency & Security:
    - Cryptographically validates the refresh token via decode_refresh_token.
    - Extracts JTI and subject user_id.
    - Queries matching RefreshToken record in PostgreSQL.
    - If session exists and belongs to the user, revokes all active tokens in its family.
    - If session was already revoked or not found, completes idempotently without error.
    - Rejects tampered or invalid tokens with InvalidRefreshTokenError.
    """
    try:
        payload = decode_refresh_token(refresh_token)
    except TokenSecurityError as exc:
        logger.info("Token revocation rejected: %s", exc)
        raise InvalidRefreshTokenError("Invalid refresh token.") from exc

    jti = payload.get("jti")
    subject = payload.get("sub")
    if not jti or not subject:
        raise InvalidRefreshTokenError("Invalid refresh token.")

    try:
        user_id = uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        raise InvalidRefreshTokenError("Invalid refresh token.") from exc

    token_session = await get_refresh_token_session_by_jti(session, str(jti))

    if token_session is not None:
        # Cross-user authorization check: session must belong to token subject
        if token_session.user_id != user_id:
            logger.warning("Revocation rejected: token subject does not match session owner")
            raise InvalidRefreshTokenError("Invalid refresh token.")

        await revoke_token_family(session, token_session.family_id)
        logger.info(
            "Token family revoked via logout: family_id=%s jti=%s",
            token_session.family_id,
            jti,
        )
    else:
        logger.info(
            "Refresh token session record not found for revocation (idempotent): jti=%s", jti
        )

    return True


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Retrieve an active or inactive user by primary key UUID."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def update_user_profile(
    session: AsyncSession,
    user: User,
    payload: UserProfileUpdateRequest,
    email_service: EmailService | None = None,
) -> User:
    """Update editable profile attributes for the authenticated user.

    Enforces:
    - Case-insensitive duplicate prevention for email and username.
    - If email changes:
        - is_verified is reset to False (requires re-verification).
        - Old verification tokens are invalidated.
        - New verification token is created for the new email.
        - Verification email is delivered post-commit.
    - If neither email nor username is changed, performs no database writes.
    - Safe concurrency handling with IntegrityError translation to 409 Conflict domain errors.
    """
    changed = False
    new_email_raw_token: str | None = None

    # Check and apply email update
    if payload.email is not None and payload.email.lower() != user.email.lower():
        existing_email = await session.execute(
            select(User.id).where(
                func.lower(User.email) == payload.email.lower(),
                User.id != user.id,
            )
        )
        if existing_email.scalar_one_or_none() is not None:
            raise DuplicateEmailError("An account with this email already exists.")

        user.email = payload.email
        user.is_verified = False

        # Invalidate old verification tokens and create new token for new email
        _token_rec, raw_token = await create_email_verification_token(
            session=session,
            user_id=user.id,
            email=user.email,
        )
        new_email_raw_token = raw_token
        changed = True

    # Check and apply username update
    if payload.username is not None and payload.username != user.username:
        if payload.username.lower() != user.username.lower():
            existing_username = await session.execute(
                select(User.id).where(
                    func.lower(User.username) == payload.username.lower(),
                    User.id != user.id,
                )
            )
            if existing_username.scalar_one_or_none() is not None:
                raise DuplicateUsernameError("An account with this username already exists.")

        user.username = payload.username
        changed = True

    if not changed:
        return user

    try:
        await session.commit()
        await session.refresh(user)
    except IntegrityError as exc:
        await session.rollback()
        err_text = str(exc.orig).lower() if hasattr(exc, "orig") else str(exc).lower()
        logger.warning(
            "Database unique constraint violation during profile update: %s",
            type(exc).__name__,
        )
        if "email" in err_text:
            raise DuplicateEmailError("An account with this email already exists.") from exc
        elif "username" in err_text:
            raise DuplicateUsernameError("An account with this username already exists.") from exc
        raise

    logger.info("Successfully updated profile for user: %s (id=%s)", user.username, user.id)

    # Post-commit verification email delivery if email changed
    if new_email_raw_token is not None:
        try:
            email_svc = email_service or get_email_service()
            await email_svc.send_verification_email(
                to_email=user.email,
                raw_token=new_email_raw_token,
            )
        except Exception as exc:
            logger.warning(
                "Non-fatal error delivering verification email after profile update for %s: %s",
                user.email,
                exc,
            )

    return user
