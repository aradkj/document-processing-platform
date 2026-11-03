"""Password hashing, verification, security policies, and JWT token management."""

import hashlib
import secrets
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.core.config import get_settings

MIN_PASSWORD_LENGTH: Final[int] = 8
MAX_PASSWORD_LENGTH: Final[int] = 128

# Standard Argon2id PasswordHasher instance
# RFC 9106 recommended parameters:
# time_cost=3, memory_cost=65536 (64 MiB), parallelism=4, hash_len=32, salt_len=16
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# Pre-computed dummy Argon2id hash for user enumeration mitigation
_DUMMY_PASSWORD_HASH: Final[str] = _hasher.hash("dummy_mitigate_enumeration_timing")


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails to satisfy security policy criteria."""

    pass


class TokenSecurityError(ValueError):
    """Base exception for token security and verification failures."""

    pass


class TokenExpiredError(TokenSecurityError):
    """Raised when a token has expired."""

    pass


class TokenInvalidError(TokenSecurityError):
    """Raised when a token signature, algorithm, claims, or format is invalid."""

    pass


def normalize_password(password: str) -> str:
    """Normalize password to Unicode NFKC form to ensure consistent byte encoding."""
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a valid string.")
    return unicodedata.normalize("NFKC", password)


def validate_password_policy(password: str) -> None:
    """Validate that a plain password conforms to platform security policy.

    Constraints:
    - Must be a valid string
    - Minimum length of 8 characters
    - Maximum length of 128 characters (mitigates DoS via oversized input)
    - Rejects empty, whitespace-only, or null-byte inputs
    """
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a string.")

    normalized = normalize_password(password)

    if "\x00" in normalized:
        raise PasswordPolicyError("Password must not contain null bytes.")

    if not normalized.strip():
        raise PasswordPolicyError("Password cannot be empty or contain only whitespace.")

    if len(normalized) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )

    if len(normalized) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password cannot exceed {MAX_PASSWORD_LENGTH} characters.")


def get_password_hash(password: str) -> str:
    """Validate a plaintext password against platform policy and generate an Argon2id hash."""
    validate_password_policy(password)
    normalized = normalize_password(password)
    try:
        return _hasher.hash(normalized)
    except HashingError as exc:
        raise RuntimeError("Cryptographic password hashing operation failed.") from exc


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a stored Argon2id hash in constant time.

    Fails safely and returns False if:
    - Either input is not a string
    - Either input is empty
    - The stored hash is malformed or invalid
    - The password does not match
    """
    if not isinstance(plain_password, str) or not isinstance(hashed_password, str):
        return False

    if not plain_password or not hashed_password:
        return False

    normalized = normalize_password(plain_password)
    try:
        return _hasher.verify(hashed_password, normalized)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        return False


def verify_dummy_password(plain_password: str) -> None:
    """Perform a constant-time dummy password verification to mitigate timing attacks."""
    verify_password(plain_password, _DUMMY_PASSWORD_HASH)


def create_access_token(
    subject: str | uuid.UUID,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a cryptographically signed JWT access token.

    Claims:
    - sub: stable identifier (user UUID string)
    - iat: issued at UTC timestamp
    - exp: expiration UTC timestamp
    - jti: unique token UUIDv4 hex
    - iss: configured issuer
    - aud: configured audience
    - type: 'access'
    """
    settings = get_settings()
    now = datetime.now(UTC)
    if expires_delta is not None:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "access",
    }

    if extra_claims:
        for key, value in extra_claims.items():
            if key not in {"sub", "iat", "exp", "type"}:
                payload[key] = value

    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT access token with algorithm pinning and claim validation.

    Enforces:
    - Strict algorithm pinning (only settings.JWT_ALGORITHM)
    - Expiration check
    - Issuer and audience validation
    - Required claims: exp, sub, iat, type
    - type claim must equal 'access'
    """
    if not isinstance(token, str) or not token.strip():
        raise TokenInvalidError("Token must be a non-empty string.")

    settings = get_settings()
    try:
        payload = jwt.decode(
            token.strip(),
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={
                "require": ["exp", "sub", "iat", "type"],
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except (
        jwt.InvalidSignatureError,
        jwt.InvalidAlgorithmError,
        jwt.DecodeError,
        jwt.InvalidAudienceError,
        jwt.InvalidIssuerError,
        jwt.MissingRequiredClaimError,
        jwt.InvalidTokenError,
    ) as exc:
        raise TokenInvalidError(f"Invalid token: {type(exc).__name__}") from exc

    if payload.get("type") != "access":
        raise TokenInvalidError("Invalid token type: expected access token.")

    subject = payload.get("sub")
    if not subject:
        raise TokenInvalidError("Token missing valid subject claim.")
    try:
        uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("Token subject is not a valid UUID.") from exc

    return payload


def create_refresh_token(
    subject: str | uuid.UUID,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
    jti: str | None = None,
) -> str:
    """Create a long-lived, cryptographically signed JWT refresh token.

    Claims:
    - sub: stable identifier (user UUID string)
    - iat: issued at UTC timestamp
    - exp: expiration UTC timestamp
    - jti: unique token UUIDv4 hex
    - iss: configured issuer
    - aud: configured audience
    - type: 'refresh'
    """
    settings = get_settings()
    now = datetime.now(UTC)
    if expires_delta is not None:
        expire = now + expires_delta
    else:
        expire = now + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)

    token_jti = jti if jti is not None else uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": token_jti,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "refresh",
    }

    if extra_claims:
        for key, value in extra_claims.items():
            if key not in {"sub", "iat", "exp", "type", "jti"}:
                payload[key] = value

    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_refresh_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT refresh token with algorithm pinning and claim validation.

    Enforces:
    - Strict algorithm pinning (only settings.JWT_ALGORITHM)
    - Expiration check
    - Issuer and audience validation
    - Required claims: exp, sub, iat, type, jti
    - type claim must equal 'refresh'
    - sub must be a valid UUID
    - jti must be a non-empty string
    """
    if not isinstance(token, str) or not token.strip():
        raise TokenInvalidError("Token must be a non-empty string.")

    settings = get_settings()
    try:
        payload = jwt.decode(
            token.strip(),
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={
                "require": ["exp", "sub", "iat", "type", "jti"],
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except (
        jwt.InvalidSignatureError,
        jwt.InvalidAlgorithmError,
        jwt.DecodeError,
        jwt.InvalidAudienceError,
        jwt.InvalidIssuerError,
        jwt.MissingRequiredClaimError,
        jwt.InvalidTokenError,
    ) as exc:
        raise TokenInvalidError(f"Invalid token: {type(exc).__name__}") from exc

    if payload.get("type") != "refresh":
        raise TokenInvalidError("Invalid token type: expected refresh token.")

    subject = payload.get("sub")
    if not subject:
        raise TokenInvalidError("Token missing valid subject claim.")
    try:
        uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("Token subject is not a valid UUID.") from exc

    jti = payload.get("jti")
    if not jti or not isinstance(jti, str) or not jti.strip():
        raise TokenInvalidError("Token missing valid jti claim.")

    return payload


def generate_email_verification_token() -> str:
    """Generate a cryptographically secure, opaque random verification token (>=256 bits)."""
    return secrets.token_urlsafe(32)


def hash_email_verification_token(raw_token: str) -> str:
    """Hash raw verification token using SHA-256 for secure, irreversible database persistence."""
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()
