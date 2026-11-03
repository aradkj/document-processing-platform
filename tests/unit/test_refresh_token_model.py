"""Unit tests for RefreshToken domain model and logout schemas."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.db.models.refresh_token import RefreshToken
from app.schemas.auth import LogoutResponse


def test_refresh_token_model_instantiation_and_defaults() -> None:
    """Verify RefreshToken model defaults, attributes, and safe representation."""
    user_id = uuid.uuid4()
    jti = uuid.uuid4().hex
    expires_at = datetime.now(UTC) + timedelta(days=7)

    token_session = RefreshToken(
        user_id=user_id,
        jti=jti,
        expires_at=expires_at,
    )

    assert token_session.id is not None
    assert token_session.user_id == user_id
    assert token_session.jti == jti
    assert token_session.family_id is not None
    assert token_session.expires_at == expires_at
    assert token_session.revoked_at is None
    assert token_session.replaced_by_jti is None
    assert token_session.is_revoked is False
    assert token_session.is_expired is False
    assert token_session.is_replaced is False
    assert token_session.is_active is True
    assert "revoked=False" in repr(token_session)
    assert "replaced=False" in repr(token_session)
    assert str(token_session.family_id) in repr(token_session)
    assert jti in repr(token_session)


def test_refresh_token_model_rotation_and_replacement() -> None:
    """Verify mark_replaced() sets replaced_by_jti, revoked_at, and is_replaced."""
    family = uuid.uuid4()
    token_session = RefreshToken(
        user_id=uuid.uuid4(),
        jti=uuid.uuid4().hex,
        family_id=family,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )

    assert token_session.is_active is True
    assert token_session.is_replaced is False
    assert token_session.family_id == family

    token_session.mark_replaced("next_jti_456")

    assert token_session.is_replaced is True
    assert token_session.is_active is False
    assert token_session.is_revoked is True
    assert token_session.revoked_at is not None
    assert token_session.replaced_by_jti == "next_jti_456"
    assert "replaced=True" in repr(token_session)


def test_refresh_token_model_revocation_behavior() -> None:
    """Verify revoke() method sets revoked_at timestamp and updates properties."""
    token_session = RefreshToken(
        user_id=uuid.uuid4(),
        jti=uuid.uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )

    assert not token_session.is_revoked
    token_session.revoke(replaced_by="new_jti_123")

    assert token_session.is_revoked is True
    assert token_session.is_active is False
    assert token_session.revoked_at is not None
    assert token_session.replaced_by_jti == "new_jti_123"
    assert "revoked=True" in repr(token_session)

    # Calling revoke again maintains original timestamp
    orig_revoked_at = token_session.revoked_at
    token_session.revoke()
    assert token_session.revoked_at == orig_revoked_at


def test_refresh_token_model_expiration_property() -> None:
    """Verify is_expired correctly identifies past expiration timestamps."""
    past_time = datetime.now(UTC) - timedelta(seconds=10)
    token_session = RefreshToken(
        user_id=uuid.uuid4(),
        jti=uuid.uuid4().hex,
        expires_at=past_time,
    )

    assert token_session.is_expired is True
    assert token_session.is_active is False


def test_logout_response_schema_defaults_and_validation() -> None:
    """Verify LogoutResponse schema validates message and forbids extra fields."""
    resp = LogoutResponse()
    assert resp.message == "Successfully logged out."

    custom_resp = LogoutResponse(message="Custom logout message.")
    assert custom_resp.message == "Custom logout message."

    with pytest.raises(ValidationError):
        LogoutResponse(extra_field="disallowed")  # type: ignore[call-arg]
