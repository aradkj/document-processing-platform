"""Unit tests for user profile request and response schemas."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.auth import UserProfileResponse, UserProfileUpdateRequest


class TestUserProfileUpdateRequestSchema:
    """Validate UserProfileUpdateRequest validation rules, normalization, and constraints."""

    def test_valid_profile_update_both_fields(self) -> None:
        """Valid email and username are accepted and normalized."""
        payload = {
            "email": "  NewUser@Example.COM  ",
            "username": "new_valid_username",
        }
        req = UserProfileUpdateRequest.model_validate(payload)
        assert req.email == "newuser@example.com"
        assert req.username == "new_valid_username"

    def test_valid_profile_update_email_only(self) -> None:
        """Partial update with only email is allowed."""
        payload = {"email": "updated@example.com"}
        req = UserProfileUpdateRequest.model_validate(payload)
        assert req.email == "updated@example.com"
        assert req.username is None

    def test_valid_profile_update_username_only(self) -> None:
        """Partial update with only username is allowed."""
        payload = {"username": "valid_user-123"}
        req = UserProfileUpdateRequest.model_validate(payload)
        assert req.email is None
        assert req.username == "valid_user-123"

    def test_empty_profile_update_payload_allowed(self) -> None:
        """Empty payload with no fields specified is valid (safe no-op)."""
        req = UserProfileUpdateRequest.model_validate({})
        assert req.email is None
        assert req.username is None
        assert req.model_fields_set == set()

    def test_explicit_null_email_rejected(self) -> None:
        """Explicitly passing email=None must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            UserProfileUpdateRequest.model_validate({"email": None})
        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "email" for e in errors)

    def test_explicit_null_username_rejected(self) -> None:
        """Explicitly passing username=None must raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            UserProfileUpdateRequest.model_validate({"username": None})
        errors = exc_info.value.errors()
        assert any(e["loc"][0] == "username" for e in errors)

    def test_explicit_null_both_fields_rejected(self) -> None:
        """Explicitly passing both fields as null must raise ValidationError for both."""
        with pytest.raises(ValidationError) as exc_info:
            UserProfileUpdateRequest.model_validate({"email": None, "username": None})
        errors = exc_info.value.errors()
        error_locs = {e["loc"][0] for e in errors}
        assert "email" in error_locs
        assert "username" in error_locs

    def test_empty_or_whitespace_only_email_rejected(self) -> None:
        """Empty or whitespace-only email string is rejected."""
        with pytest.raises(ValidationError):
            UserProfileUpdateRequest.model_validate({"email": ""})
        with pytest.raises(ValidationError):
            UserProfileUpdateRequest.model_validate({"email": "   "})

    def test_email_normalization_strips_whitespace_and_lowercases(self) -> None:
        """Email is trimmed and converted to lowercase."""
        req = UserProfileUpdateRequest.model_validate({"email": "   TEST.User+1@DOMAIN.COM  "})
        assert req.email == "test.user+1@domain.com"

    @pytest.mark.parametrize(
        "invalid_email",
        [
            "not-an-email",
            "@missing-local.com",
            "missing-domain@",
            "spaces in@email.com",
        ],
    )
    def test_invalid_email_format_rejected(self, invalid_email: str) -> None:
        """Malformed email strings raise ValidationError."""
        with pytest.raises(ValidationError):
            UserProfileUpdateRequest.model_validate({"email": invalid_email})

    @pytest.mark.parametrize(
        "valid_username",
        [
            "abc",
            "user_name",
            "user-name",
            "USER_123",
            "a" * 50,
        ],
    )
    def test_valid_username_formats(self, valid_username: str) -> None:
        """Valid usernames matching policy pass validation."""
        req = UserProfileUpdateRequest.model_validate({"username": valid_username})
        assert req.username == valid_username.strip()

    @pytest.mark.parametrize(
        "invalid_username",
        [
            "ab",  # too short (<3)
            "a" * 51,  # too long (>50)
            "user name",  # contains space
            "user@name",  # invalid symbol
            "user#name",  # invalid symbol
            "user!name",  # invalid symbol
            "user$name",  # invalid symbol
        ],
    )
    def test_invalid_username_formats_rejected(self, invalid_username: str) -> None:
        """Usernames violating policy raise ValidationError."""
        with pytest.raises(ValidationError):
            UserProfileUpdateRequest.model_validate({"username": invalid_username})

    @pytest.mark.parametrize(
        "forbidden_field,value",
        [
            ("is_active", False),
            ("is_verified", True),
            ("roles", ["admin"]),
            ("password", "Secret123!"),
            ("password_hash", "$argon2id$..."),
            ("id", str(uuid.uuid4())),
            ("user_id", str(uuid.uuid4())),
            ("created_at", datetime.now(UTC).isoformat()),
            ("updated_at", datetime.now(UTC).isoformat()),
            ("last_login_at", datetime.now(UTC).isoformat()),
            ("permissions", ["users:read"]),
        ],
    )
    def test_forbidden_and_extra_fields_strictly_rejected(
        self, forbidden_field: str, value: object
    ) -> None:
        """Any extra or sensitive fields are strictly rejected via extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            UserProfileUpdateRequest.model_validate({forbidden_field: value})
        errors = exc_info.value.errors()
        forbidden_keys = {e["loc"][0] for e in errors}
        assert forbidden_field in forbidden_keys


class TestUserProfileResponseSchema:
    """Validate UserProfileResponse serialization and safety properties."""

    def test_valid_user_profile_response(self) -> None:
        """UserProfileResponse models public user profile attributes."""
        now = datetime.now(UTC)
        user_id = uuid.uuid4()
        res = UserProfileResponse(
            id=user_id,
            email="user@example.com",
            username="testuser",
            is_active=True,
            is_verified=True,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        assert res.id == user_id
        assert res.email == "user@example.com"
        assert res.username == "testuser"
        assert res.is_active is True
        assert res.is_verified is True
        assert res.created_at == now
        assert res.updated_at == now
        assert res.last_login_at == now

    def test_response_never_contains_sensitive_fields(self) -> None:
        """UserProfileResponse model schema has no password, hash, or token fields."""
        fields = set(UserProfileResponse.model_fields.keys())
        sensitive_fields = {
            "password",
            "password_hash",
            "refresh_token",
            "access_token",
            "jti",
            "family_id",
            "token_family",
        }
        intersection = fields.intersection(sensitive_fields)
        assert not intersection, f"Sensitive fields found in response schema: {intersection}"
