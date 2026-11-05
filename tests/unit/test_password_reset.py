"""Unit tests for password reset tokens, security hashing, schemas, and email templates."""

import uuid
from datetime import UTC, datetime, timedelta
from email.message import Message

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.security import (
    generate_password_reset_token,
    hash_password_reset_token,
)
from app.db.models.password_reset_token import PasswordResetToken
from app.schemas.auth import (
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
)
from app.services.email import EmailService


class TestPasswordResetTokenSecurity:
    """Verify cryptographically secure token generation and SHA-256 hashing."""

    def test_token_generation_length_and_entropy(self) -> None:
        """Token generator must produce URL-safe tokens with >= 256 bits of entropy."""
        token1 = generate_password_reset_token()
        token2 = generate_password_reset_token()

        assert len(token1) >= 43  # 32 urlsafe bytes produce 43+ chars
        assert len(token2) >= 43
        assert token1 != token2

    def test_token_hashing_sha256_properties(self) -> None:
        """Token hashing must produce standard 64-character hexadecimal SHA-256 digest."""
        raw_token = generate_password_reset_token()
        token_hash = hash_password_reset_token(raw_token)

        assert len(token_hash) == 64
        assert token_hash != raw_token
        # Hexadecimal characters only
        assert all(c in "0123456789abcdef" for c in token_hash)

    def test_different_tokens_produce_different_hashes(self) -> None:
        """Different tokens must always produce distinct SHA-256 hashes."""
        t1 = generate_password_reset_token()
        t2 = generate_password_reset_token()
        assert hash_password_reset_token(t1) != hash_password_reset_token(t2)

    def test_token_hashing_trims_whitespace(self) -> None:
        """Whitespace around raw tokens must be trimmed prior to hashing."""
        token = generate_password_reset_token()
        hash1 = hash_password_reset_token(token)
        hash2 = hash_password_reset_token(f"  {token}  \n")
        assert hash1 == hash2


class TestPasswordResetTokenModel:
    """Verify PasswordResetToken model constraints, state properties, and representation safety."""

    def test_model_defaults_and_attributes(self) -> None:
        """Verify model attribute storage and properties."""
        user_id = uuid.uuid4()
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=30)
        token = PasswordResetToken(
            user_id=user_id,
            email="user@example.com",
            token_hash="a" * 64,
            expires_at=expires_at,
            created_at=now,
        )

        assert token.user_id == user_id
        assert token.email == "user@example.com"
        assert token.token_hash == "a" * 64
        assert token.used_at is None
        assert token.invalidated_at is None
        assert token.is_active is True
        assert token.is_expired is False
        assert token.is_used is False

    def test_model_expiration_property(self) -> None:
        """Verify is_expired correctly reflects UTC expiration timestamp."""
        now = datetime.now(UTC)
        token = PasswordResetToken(
            user_id=uuid.uuid4(),
            email="user@example.com",
            token_hash="b" * 64,
            expires_at=now - timedelta(seconds=1),
            created_at=now - timedelta(minutes=10),
        )
        assert token.is_expired is True
        assert token.is_active is False

    def test_model_used_property(self) -> None:
        """Verify is_used returns True when used_at is set."""
        now = datetime.now(UTC)
        token = PasswordResetToken(
            user_id=uuid.uuid4(),
            email="user@example.com",
            token_hash="c" * 64,
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            used_at=now + timedelta(minutes=5),
        )
        assert token.is_used is True
        assert token.is_active is False

    def test_model_invalidated_property(self) -> None:
        """Verify is_active returns False when invalidated_at is set."""
        now = datetime.now(UTC)
        token = PasswordResetToken(
            user_id=uuid.uuid4(),
            email="user@example.com",
            token_hash="d" * 64,
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            invalidated_at=now + timedelta(minutes=2),
        )
        assert token.is_active is False

    def test_repr_does_not_leak_token_hash(self) -> None:
        """The __repr__ string must not contain token hash or sensitive data."""
        secret_hash = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
        token = PasswordResetToken(
            user_id=uuid.uuid4(),
            email="user@example.com",
            token_hash=secret_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        repr_str = repr(token)
        assert secret_hash not in repr_str
        assert "PasswordResetToken" in repr_str


class TestPasswordResetSchemas:
    """Verify request and response Pydantic validation schemas."""

    def test_valid_password_reset_request(self) -> None:
        """Valid email is accepted and normalized."""
        req = PasswordResetRequest(email="  USER@Example.COM  ")
        assert req.email == "user@example.com"

    def test_empty_email_rejected(self) -> None:
        """Empty or invalid email raises validation error."""
        with pytest.raises(ValidationError):
            PasswordResetRequest(email="not-an-email")

    def test_password_reset_request_extra_fields_forbidden(self) -> None:
        """Extra fields are rejected."""
        with pytest.raises(ValidationError):
            PasswordResetRequest.model_validate(
                {"email": "user@example.com", "role": "admin"},
                context={"extra": "forbid"},
            )

    def test_password_reset_request_response_default_message(self) -> None:
        """Default anti-enumeration generic message is provided."""
        resp = PasswordResetRequestResponse()
        assert "password reset instructions will be sent" in resp.message.lower()

    def test_valid_password_reset_confirm_request(self) -> None:
        """Valid confirm request passes schema validation."""
        req = PasswordResetConfirmRequest(
            token="valid_raw_token_string_here_123",
            new_password="NewSecurePassword123!",
        )
        assert req.token == "valid_raw_token_string_here_123"
        assert req.new_password == "NewSecurePassword123!"

    @pytest.mark.parametrize("bad_token", ["", "   "])
    def test_empty_token_rejected_in_confirm(self, bad_token: str) -> None:
        """Empty or whitespace-only token is rejected."""
        with pytest.raises(ValidationError):
            PasswordResetConfirmRequest(token=bad_token, new_password="NewSecurePassword123!")

    def test_weak_password_rejected_in_confirm(self) -> None:
        """Weak password failing policy is rejected."""
        with pytest.raises(ValidationError):
            PasswordResetConfirmRequest(token="token123", new_password="weak")

    def test_confirm_extra_fields_forbidden(self) -> None:
        """Extra fields in confirm request are forbidden."""
        with pytest.raises(ValidationError):
            PasswordResetConfirmRequest.model_validate(
                {"token": "token123", "new_password": "NewSecurePassword123!", "extra": "evil"},
            )

    def test_password_reset_confirm_response_default(self) -> None:
        """Default confirmation message indicates success."""
        resp = PasswordResetConfirmResponse()
        assert "password has been reset successfully" in resp.message.lower()


class TestPasswordResetEmailService:
    """Verify password reset email URL generation and template formatting."""

    def test_reset_url_construction(self) -> None:
        """Reset URL must place raw token in URI fragment (#token=...)."""
        service = EmailService(use_mock_transport=True)
        raw_token = "abc123XYZ_SecretToken"
        url = service.build_password_reset_url(raw_token)

        settings = get_settings()
        expected_base = settings.FRONTEND_BASE_URL.rstrip("/")
        assert url == f"{expected_base}/reset-password#token={raw_token}"
        # Token must NOT be in query parameter (?token=)
        assert "?token=" not in url
        assert "#token=" in url

    def test_create_password_reset_message_structure(self) -> None:
        """MIME message must contain both plain-text and HTML alternatives with safe reset URL."""
        service = EmailService(use_mock_transport=True)
        raw_token = "secure_token_abc"
        url = service.build_password_reset_url(raw_token)

        msg = service.create_password_reset_message(
            to_email="user@example.com",
            reset_url=url,
            expires_in_minutes=30,
        )

        assert msg["To"] == "user@example.com"
        assert "Reset your password" in msg["Subject"]
        assert msg.is_multipart() is True

        payload = msg.get_payload()
        assert isinstance(payload, list)
        payloads: list[str] = []
        for part in payload:
            assert isinstance(part, Message)
            raw = part.get_payload(decode=True)
            assert isinstance(raw, bytes)
            payloads.append(raw.decode("utf-8"))

        # Both parts must contain the reset URL
        assert any(url in p for p in payloads)
        # Expiration mention
        assert any("30 minutes" in p for p in payloads)

    @pytest.mark.asyncio
    async def test_mock_transport_records_password_reset(self) -> None:
        """When mock transport is enabled, messages are recorded in sent_messages."""
        service = EmailService(use_mock_transport=True)
        raw_token = "test_mock_raw_token"
        result = await service.send_password_reset_email(
            to_email="test@example.com",
            raw_token=raw_token,
            expires_in_minutes=30,
        )
        assert result is True
        assert len(service.sent_messages) == 1
        msg = service.sent_messages[0]
        assert msg["to_email"] == "test@example.com"
        assert msg["type"] == "password_reset"
        assert raw_token in msg["reset_url"]
