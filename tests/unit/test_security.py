"""Unit tests for password hashing, policy validation, and verification."""

import pytest

from app.core.security import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    get_password_hash,
    normalize_password,
    validate_password_policy,
    verify_password,
)
from app.db.models.user import User


def test_get_password_hash_produces_valid_argon2id_hash() -> None:
    """Verify that hashing produces a standard Argon2id hash with random salting."""
    plain = "SuperSecurePassword123!"
    hash1 = get_password_hash(plain)
    hash2 = get_password_hash(plain)

    # Must be valid Argon2id format
    assert hash1.startswith("$argon2id$")
    assert hash2.startswith("$argon2id$")

    # Plaintext must never equal the hash
    assert hash1 != plain
    assert hash2 != plain

    # Two separate hash operations on the same password must produce distinct salts
    assert hash1 != hash2


def test_verify_password_correct_and_incorrect() -> None:
    """Verify password verification behavior with matching and non-matching inputs."""
    plain = "MySecretPassword2026@"
    stored_hash = get_password_hash(plain)

    # Correct password succeeds
    assert verify_password(plain, stored_hash) is True

    # Incorrect passwords fail
    assert verify_password("WrongPassword123", stored_hash) is False
    assert verify_password("mysecretpassword2026@", stored_hash) is False
    assert verify_password(plain + " ", stored_hash) is False


def test_verify_password_malformed_hashes_fail_safely() -> None:
    """Verify that invalid or malformed hashes return False without raising exceptions."""
    plain = "SafePassword123"

    assert verify_password(plain, "not_a_valid_hash") is False
    assert verify_password(plain, "$argon2id$malformed$parameters$string") is False
    assert verify_password(plain, "") is False
    assert verify_password("", "$argon2id$somehash") is False
    assert verify_password(plain, None) is False  # type: ignore[arg-type]
    assert verify_password(None, "somehash") is False  # type: ignore[arg-type]


def test_password_policy_minimum_and_maximum_length() -> None:
    """Verify policy rejects passwords that are too short or too long."""
    # Too short (< 8 chars)
    short_pw = "Short1"
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy(short_pw)
    assert f"at least {MIN_PASSWORD_LENGTH}" in str(exc_info.value)

    # Too long (> 128 chars)
    long_pw = "A" * (MAX_PASSWORD_LENGTH + 1)
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy(long_pw)
    assert f"cannot exceed {MAX_PASSWORD_LENGTH}" in str(exc_info.value)

    # Exactly 8 chars (valid)
    validate_password_policy("Valid008")

    # Exactly 128 chars (valid)
    validate_password_policy("X" * MAX_PASSWORD_LENGTH)


def test_password_policy_rejects_empty_and_whitespace_only() -> None:
    """Verify policy rejects empty or blank whitespace strings."""
    for invalid_pw in ["", "   ", "\t\t\t\t\t\t\t\t", "\n\n\n\n\n\n\n\n"]:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_policy(invalid_pw)
        assert "cannot be empty" in str(exc_info.value)


def test_password_policy_rejects_null_bytes() -> None:
    """Verify policy rejects passwords containing null bytes."""
    with pytest.raises(PasswordPolicyError) as exc_info:
        validate_password_policy("Password\x00WithNull")
    assert "null byte" in str(exc_info.value)


def test_password_policy_error_does_not_leak_plaintext_password() -> None:
    """Verify that PasswordPolicyError message never reveals the attempted password."""
    sensitive_attempt = "bad123"
    with pytest.raises(PasswordPolicyError) as exc_info:
        get_password_hash(sensitive_attempt)

    error_msg = str(exc_info.value)
    assert sensitive_attempt not in error_msg


def test_unicode_password_normalization_and_verification() -> None:
    """Verify that Unicode passwords (e.g. Persian/German characters) hash and verify properly."""
    persian_pw = "رمز_عبور_بسیار_امن_۱۴۰۵"
    persian_hash = get_password_hash(persian_pw)

    assert verify_password(persian_pw, persian_hash) is True
    assert verify_password("رمز_عبور_اشتباه_۱۴۰۵", persian_hash) is False

    # NFKC compatibility normalization
    german_pw = "München-Späti-2026"
    german_hash = get_password_hash(german_pw)
    assert verify_password(german_pw, german_hash) is True
    assert verify_password(normalize_password(german_pw), german_hash) is True


def test_user_model_integration_with_argon2id_hash() -> None:
    """Verify that generated Argon2id hashes seamlessly integrate with User entity."""
    plain = "PlatformUserPass2026!"
    hashed = get_password_hash(plain)

    user = User(
        email="argon2user@example.com",
        username="argon2user",
        password_hash=hashed,
    )

    # Verify column capacity
    assert len(hashed) <= 255
    assert user.password_hash == hashed
    assert verify_password(plain, user.password_hash) is True
    assert verify_password("WrongPassword", user.password_hash) is False

    # Verify repr does not leak
    assert hashed not in repr(user)
    assert plain not in repr(user)
