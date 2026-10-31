"""Password hashing, verification, and security policies using Argon2id."""

import unicodedata
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import (
    HashingError,
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

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


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails to satisfy security policy criteria."""

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
