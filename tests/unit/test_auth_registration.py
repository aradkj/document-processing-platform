"""Unit tests for user registration schemas, validation, and security boundaries."""

import pytest
from pydantic import ValidationError

from app.db.models.user import User
from app.schemas.auth import UserRegisterRequest, UserRegisterResponse


class TestUserRegisterRequestSchema:
    """Validate UserRegisterRequest Pydantic schema constraints."""

    def test_valid_request(self) -> None:
        """Valid registration request parses and normalizes email and username."""
        payload = {
            "email": "  Alice@Example.com  ",
            "username": "  alice_99  ",
            "password": "CorrectHorseBatteryStaple1!",
        }
        req = UserRegisterRequest.model_validate(payload)
        assert req.email == "alice@example.com"
        assert req.username == "alice_99"
        assert req.password == "CorrectHorseBatteryStaple1!"

    def test_extra_fields_forbidden(self) -> None:
        """Client cannot supply role, is_admin, id, or other unauthorized fields."""
        payload = {
            "email": "attacker@example.com",
            "username": "attacker",
            "password": "SecurePassword123!",
            "role": "admin",
            "is_admin": True,
            "is_superuser": True,
            "id": "00000000-0000-0000-0000-000000000001",
            "password_hash": "precomputed_hash",
        }
        with pytest.raises(ValidationError) as exc_info:
            UserRegisterRequest.model_validate(payload)
        errors = exc_info.value.errors()
        forbidden_fields = {e["loc"][0] for e in errors}
        assert "role" in forbidden_fields
        assert "is_admin" in forbidden_fields
        assert "password_hash" in forbidden_fields

    @pytest.mark.parametrize(
        "invalid_email",
        [
            "not-an-email",
            "@missinguser.com",
            "missingdomain@",
            "user@.com",
            "user@domain..com",
            "spaces in@domain.com",
            "",
            "   ",
        ],
    )
    def test_invalid_email_rejected(self, invalid_email: str) -> None:
        """Invalid email addresses are rejected by EmailStr validation."""
        payload = {
            "email": invalid_email,
            "username": "valid_user",
            "password": "SecurePassword123!",
        }
        with pytest.raises(ValidationError):
            UserRegisterRequest.model_validate(payload)

    @pytest.mark.parametrize(
        "invalid_username",
        [
            "a",  # too short (< 3)
            "ab",  # too short (< 3)
            "a" * 51,  # too long (> 50)
            "user@name",  # invalid character @
            "user name",  # space not allowed
            "user!name",  # exclamation not allowed
            "user.name",  # dot not in allowed set [a-zA-Z0-9_-]
            "",  # empty
            "   ",  # whitespace only
        ],
    )
    def test_invalid_username_rejected(self, invalid_username: str) -> None:
        """Invalid usernames failing policy are rejected with ValidationError."""
        payload = {
            "email": "user@example.com",
            "username": invalid_username,
            "password": "SecurePassword123!",
        }
        with pytest.raises(ValidationError):
            UserRegisterRequest.model_validate(payload)

    @pytest.mark.parametrize(
        "valid_username",
        [
            "abc",
            "john_doe",
            "alice-smith",
            "User_12345",
            "a" * 50,
        ],
    )
    def test_valid_usernames_accepted(self, valid_username: str) -> None:
        """Valid usernames following policy are accepted."""
        payload = {
            "email": "user@example.com",
            "username": valid_username,
            "password": "SecurePassword123!",
        }
        req = UserRegisterRequest.model_validate(payload)
        assert req.username == valid_username

    @pytest.mark.parametrize(
        "bad_password,expected_error",
        [
            ("short", "Password must be at least 8 characters long."),
            ("a" * 129, "Password cannot exceed 128 characters."),
            ("", "Password cannot be empty or contain only whitespace."),
            ("       ", "Password cannot be empty or contain only whitespace."),
            ("pass\x00word", "Password must not contain null bytes."),
        ],
    )
    def test_password_policy_failures_rejected(
        self, bad_password: str, expected_error: str
    ) -> None:
        """Passwords failing core policy are rejected at schema validation."""
        payload = {
            "email": "user@example.com",
            "username": "valid_user",
            "password": bad_password,
        }
        with pytest.raises(ValidationError) as exc_info:
            UserRegisterRequest.model_validate(payload)
        assert any(expected_error in str(err) for err in exc_info.value.errors())

    def test_unicode_normalized_password(self) -> None:
        """Passwords with Unicode characters pass and normalize correctly."""
        payload = {
            "email": "user@example.com",
            "username": "valid_user",
            "password": "P\u00e1ssw\u00f6rd123!",  # Pásswörd123!
        }
        req = UserRegisterRequest.model_validate(payload)
        assert req.password == "P\u00e1ssw\u00f6rd123!"


class TestUserRegisterResponseSchema:
    """Validate UserRegisterResponse security boundaries."""

    def test_response_never_contains_password_fields(self) -> None:
        """Response model must not have password or password_hash fields."""
        fields = UserRegisterResponse.model_fields.keys()
        assert "password" not in fields
        assert "password_hash" not in fields

    def test_user_repr_never_leaks_password_hash(self) -> None:
        """User model __repr__ does not include password or password_hash."""
        user = User(
            email="repr_test@example.com",
            username="repr_tester",
            password_hash="$argon2id$v=19$m=65536,t=3,p=4$secret_hash_value",
        )
        repr_str = repr(user)
        assert "secret_hash_value" not in repr_str
        assert "password_hash" not in repr_str
        assert "repr_test@example.com" in repr_str
