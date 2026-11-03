"""Unit tests for login request, token response, and profile schemas."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.auth import (
    RefreshTokenRequest,
    TokenResponse,
    UserLoginRequest,
    UserMeResponse,
)


class TestUserLoginRequestSchema:
    """Validate UserLoginRequest constraints and normalization."""

    def test_valid_login_request_normalizes_email(self) -> None:
        """Valid login request strips whitespace and converts email to lowercase."""
        payload = {
            "email": "  Alice@Example.COM  ",
            "password": "MySecretPassword123!",
        }
        req = UserLoginRequest.model_validate(payload)
        assert req.email == "alice@example.com"
        assert req.password == "MySecretPassword123!"

    def test_extra_fields_forbidden(self) -> None:
        """Unexpected extra fields like role or is_admin are rejected."""
        payload = {
            "email": "user@example.com",
            "password": "MySecretPassword123!",
            "role": "admin",
            "is_admin": True,
        }
        with pytest.raises(ValidationError) as exc_info:
            UserLoginRequest.model_validate(payload)
        errors = exc_info.value.errors()
        forbidden_fields = {e["loc"][0] for e in errors}
        assert "role" in forbidden_fields
        assert "is_admin" in forbidden_fields

    def test_invalid_email_format_rejected(self) -> None:
        """Obviously invalid email format is rejected."""
        with pytest.raises(ValidationError):
            UserLoginRequest.model_validate({"email": "not-an-email", "password": "password123"})


class TestRefreshTokenRequestSchema:
    """Validate RefreshTokenRequest constraints."""

    def test_valid_refresh_token_request(self) -> None:
        """Valid refresh token request passes and trims whitespace."""
        req = RefreshTokenRequest.model_validate({"refresh_token": "  valid.refresh.token  "})
        assert req.refresh_token == "valid.refresh.token"

    def test_extra_fields_forbidden(self) -> None:
        """Extra fields on refresh token request are rejected."""
        with pytest.raises(ValidationError):
            RefreshTokenRequest.model_validate(
                {"refresh_token": "token", "extra_param": "attacker"}
            )

    @pytest.mark.parametrize("empty_token", ["", "   "])
    def test_empty_refresh_token_rejected(self, empty_token: str) -> None:
        """Blank or empty refresh token strings are rejected."""
        with pytest.raises(ValidationError):
            RefreshTokenRequest.model_validate({"refresh_token": empty_token})


class TestTokenResponseSchema:
    """Validate TokenResponse constraints."""

    def test_valid_token_response(self) -> None:
        """TokenResponse properly models access and refresh token metadata."""
        res = TokenResponse(
            access_token="header.payload.signature",
            refresh_token="header.refresh.signature",
            token_type="bearer",
            expires_in=1800,
        )
        assert res.access_token == "header.payload.signature"
        assert res.refresh_token == "header.refresh.signature"
        assert res.token_type == "bearer"
        assert res.expires_in == 1800

    def test_token_response_optional_refresh_token(self) -> None:
        """TokenResponse can omit refresh_token when refreshing access token."""
        res = TokenResponse(
            access_token="header.payload.signature",
            token_type="bearer",
            expires_in=1800,
        )
        assert res.access_token == "header.payload.signature"
        assert res.refresh_token is None

    def test_token_response_never_contains_password_fields(self) -> None:
        """TokenResponse model schema has no password or sensitive credentials."""
        fields = TokenResponse.model_fields.keys()
        assert "password" not in fields
        assert "password_hash" not in fields


class TestUserMeResponseSchema:
    """Validate UserMeResponse constraints."""

    def test_valid_user_me_response(self) -> None:
        """UserMeResponse models authenticated user profile."""
        now = datetime.now(UTC)
        res = UserMeResponse(
            id=uuid.uuid4(),
            email="me@example.com",
            username="me_user",
            is_active=True,
            is_verified=False,
            last_login_at=now,
            created_at=now,
        )
        assert res.email == "me@example.com"
        assert res.username == "me_user"
        assert res.last_login_at == now

    def test_user_me_response_never_contains_password_fields(self) -> None:
        """UserMeResponse model fields must not include password or password_hash."""
        fields = UserMeResponse.model_fields.keys()
        assert "password" not in fields
        assert "password_hash" not in fields
