"""Unit tests for email verification token security, models, schemas, and email delivery."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.mime.multipart import MIMEMultipart
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import (
    generate_email_verification_token,
    hash_email_verification_token,
)
from app.db.models.email_verification_token import EmailVerificationToken
from app.schemas.auth import (
    ResendVerificationResponse,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.services.email import EmailService


class TestVerificationTokenSecurity:
    """Validate token generation randomness, entropy, and cryptographic hashing."""

    def test_token_generation_length_and_entropy(self) -> None:
        """Token generator produces high-entropy URL-safe strings."""
        token1 = generate_email_verification_token()
        token2 = generate_email_verification_token()

        assert isinstance(token1, str)
        assert isinstance(token2, str)
        assert token1 != token2
        # secrets.token_urlsafe(32) produces 43-44 base64 chars (256 bits of entropy)
        assert len(token1) >= 42
        assert len(token2) >= 42

    def test_token_hashing_sha256_properties(self) -> None:
        """Hashed tokens are 64-char hexadecimal SHA-256 strings and deterministic."""
        raw_token = "secure_test_token_sample_1234567890"
        hash1 = hash_email_verification_token(raw_token)
        hash2 = hash_email_verification_token(raw_token)

        assert hash1 == hash2
        assert len(hash1) == 64
        assert hash1 == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def test_different_tokens_produce_different_hashes(self) -> None:
        """Distinct raw tokens yield distinct SHA-256 hashes."""
        token_a = generate_email_verification_token()
        token_b = generate_email_verification_token()

        assert hash_email_verification_token(token_a) != hash_email_verification_token(token_b)

    def test_token_hashing_trims_whitespace(self) -> None:
        """Hash function automatically trims leading and trailing whitespace."""
        raw = "my_token_value_abc"
        with_spaces = "   my_token_value_abc   "
        assert hash_email_verification_token(raw) == hash_email_verification_token(with_spaces)


class TestEmailVerificationTokenModel:
    """Validate EmailVerificationToken ORM model properties and lifecycle state."""

    def test_model_defaults_and_attributes(self) -> None:
        """Model instantiates with proper attributes, timestamps, and active status."""
        user_id = uuid.uuid4()
        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=30)
        token_hash = hash_email_verification_token("raw_tok")

        token = EmailVerificationToken(
            user_id=user_id,
            email="user@example.com",
            token_hash=token_hash,
            expires_at=expiry,
        )

        assert token.id is not None
        assert token.user_id == user_id
        assert token.email == "user@example.com"
        assert token.token_hash == token_hash
        assert token.expires_at == expiry
        assert token.used_at is None
        assert token.invalidated_at is None
        assert token.is_expired is False
        assert token.is_active is True

    def test_model_expiration_property(self) -> None:
        """Expired token correctly returns is_expired=True and is_active=False."""
        user_id = uuid.uuid4()
        past_expiry = datetime.now(UTC) - timedelta(minutes=5)

        token = EmailVerificationToken(
            user_id=user_id,
            email="user@example.com",
            token_hash="sample_hash",
            expires_at=past_expiry,
        )
        assert token.is_expired is True
        assert token.is_active is False

    def test_model_used_property(self) -> None:
        """Consumed token has is_active=False."""
        user_id = uuid.uuid4()
        expiry = datetime.now(UTC) + timedelta(minutes=30)

        token = EmailVerificationToken(
            user_id=user_id,
            email="user@example.com",
            token_hash="sample_hash",
            expires_at=expiry,
            used_at=datetime.now(UTC),
        )
        assert token.used_at is not None
        assert token.is_active is False

    def test_model_invalidated_property(self) -> None:
        """Invalidated/superseded token has is_active=False."""
        user_id = uuid.uuid4()
        expiry = datetime.now(UTC) + timedelta(minutes=30)

        token = EmailVerificationToken(
            user_id=user_id,
            email="user@example.com",
            token_hash="sample_hash",
            expires_at=expiry,
            invalidated_at=datetime.now(UTC),
        )
        assert token.invalidated_at is not None
        assert token.is_active is False

    def test_repr_does_not_leak_token_hash(self) -> None:
        """String representation of model hides sensitive token hash."""
        secret_hash = "abcdef1234567890secret_hash_not_to_leak"
        token = EmailVerificationToken(
            user_id=uuid.uuid4(),
            email="user@example.com",
            token_hash=secret_hash,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        rep = repr(token)
        assert secret_hash not in rep
        assert "user@example.com" in rep


class TestEmailVerificationSchemas:
    """Validate Pydantic request and response schemas."""

    def test_valid_verify_email_request(self) -> None:
        """Valid token passes request schema validation and trims whitespace."""
        req = VerifyEmailRequest.model_validate({"token": "  sample_valid_token_123  "})
        assert req.token == "sample_valid_token_123"

    @pytest.mark.parametrize("empty_token", ["", "   "])
    def test_empty_or_whitespace_token_rejected(self, empty_token: str) -> None:
        """Empty or whitespace-only verification tokens raise ValidationError."""
        with pytest.raises(ValidationError):
            VerifyEmailRequest.model_validate({"token": empty_token})

    def test_extra_fields_forbidden(self) -> None:
        """Unexpected extra fields are strictly forbidden."""
        with pytest.raises(ValidationError):
            VerifyEmailRequest.model_validate({"token": "tok", "user_id": str(uuid.uuid4())})

    def test_verify_email_response_default(self) -> None:
        """VerifyEmailResponse default message is correct."""
        res = VerifyEmailResponse()
        assert res.message == "Email verified successfully."

    def test_resend_verification_response_default(self) -> None:
        """ResendVerificationResponse default message is correct."""
        res = ResendVerificationResponse()
        assert res.message == "Verification email sent if account is unverified."


class TestEmailService:
    """Validate EmailService message construction, URL generation, and error resilience."""

    def test_verification_url_construction(self) -> None:
        """Verification URL combines frontend base URL with raw token in URI fragment."""
        settings = Settings(FRONTEND_BASE_URL="https://app.example.com")
        service = EmailService(settings=settings)
        url = service.build_verification_url("my_test_token_abc")
        assert url == "https://app.example.com/verify-email#token=my_test_token_abc"

    def test_create_verification_message_headers_and_content(self) -> None:
        """Multipart MIME message contains expected headers, plain text, and HTML parts."""
        settings = Settings(
            APP_NAME="DocPlatform Test",
            SMTP_FROM_EMAIL="noreply@test.local",
            SMTP_FROM_NAME="Test Sender",
        )
        service = EmailService(settings=settings)
        msg = service.create_verification_message(
            to_email="recipient@example.com",
            verification_url="https://app.example.com/verify-email#token=xyz",
            expires_in_minutes=30,
        )

        assert isinstance(msg, MIMEMultipart)
        assert msg["Subject"] == "Verify your email - DocPlatform Test"
        assert "recipient@example.com" in msg["To"]
        assert "noreply@test.local" in msg["From"]

        # Verify payload parts
        payload = msg.get_payload()
        assert isinstance(payload, list)
        assert len(payload) == 2

        part0 = payload[0]
        part1 = payload[1]
        assert isinstance(part0, Message)
        assert isinstance(part1, Message)
        raw_text = part0.get_payload(decode=True)
        raw_html = part1.get_payload(decode=True)
        assert isinstance(raw_text, bytes)
        assert isinstance(raw_html, bytes)
        text_content = raw_text.decode("utf-8")
        html_content = raw_html.decode("utf-8")

        assert "https://app.example.com/verify-email#token=xyz" in text_content
        assert "30 minutes" in text_content
        assert "recipient@example.com" in text_content

        assert "https://app.example.com/verify-email#token=xyz" in html_content
        assert "30 minutes" in html_content
        assert "recipient@example.com" in html_content

    @pytest.mark.asyncio
    async def test_mock_transport_recording(self) -> None:
        """Mock transport records sent messages without network calls."""
        service = EmailService(use_mock_transport=True)
        delivered = await service.send_verification_email(
            to_email="test@example.com",
            raw_token="raw_token_value",
            expires_in_minutes=15,
        )
        assert delivered is True
        assert len(service.sent_messages) == 1
        record = service.sent_messages[0]
        assert record["to_email"] == "test@example.com"
        assert "raw_token_value" in record["verification_url"]
        assert record["expires_in_minutes"] == 15

    @pytest.mark.asyncio
    async def test_smtp_failure_is_handled_safely(self) -> None:
        """SMTP network exceptions return False without raising unhandled errors."""
        service = EmailService(use_mock_transport=False)
        with patch.object(
            service, "_send_smtp_sync", side_effect=ConnectionRefusedError("SMTP down")
        ):
            delivered = await service.send_verification_email(
                to_email="fail@example.com",
                raw_token="raw_token_xyz",
            )
            assert delivered is False
