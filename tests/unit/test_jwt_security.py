"""Unit tests for JWT access token creation, verification, claims, and security boundaries."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    verify_dummy_password,
)


def test_create_access_token_structure_and_claims() -> None:
    """Verify that generated JWT contains all mandatory claims with correct values."""
    settings = get_settings()
    user_id = uuid.uuid4()
    before_ts = int(datetime.now(UTC).timestamp())

    token = create_access_token(subject=user_id)
    after_ts = int(datetime.now(UTC).timestamp())

    # Raw decode without verification to inspect raw claims
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["sub"] == str(user_id)
    assert claims["type"] == "access"
    assert claims["iss"] == settings.JWT_ISSUER
    assert claims["aud"] == settings.JWT_AUDIENCE
    assert "jti" in claims
    assert len(claims["jti"]) == 32  # uuid.hex

    assert before_ts <= claims["iat"] <= after_ts
    expected_exp = claims["iat"] + (settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    assert claims["exp"] == expected_exp


def test_create_access_token_with_custom_expires_delta() -> None:
    """Verify that a custom expiration delta is properly honored."""
    user_id = str(uuid.uuid4())
    custom_delta = timedelta(minutes=15)

    token = create_access_token(subject=user_id, expires_delta=custom_delta)
    claims = decode_access_token(token)

    expected_duration = int(custom_delta.total_seconds())
    actual_duration = claims["exp"] - claims["iat"]
    assert actual_duration == expected_duration


def test_decode_access_token_success() -> None:
    """Verify that a valid token decodes successfully through the security abstraction."""
    user_id = uuid.uuid4()
    token = create_access_token(subject=user_id)

    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"


def test_decode_access_token_rejects_expired() -> None:
    """Verify that expired tokens raise TokenExpiredError."""
    user_id = uuid.uuid4()
    # Expire 1 minute in the past
    expired_token = create_access_token(
        subject=user_id,
        expires_delta=timedelta(minutes=-1),
    )

    with pytest.raises(TokenExpiredError) as exc_info:
        decode_access_token(expired_token)
    assert "expired" in str(exc_info.value).lower()


def test_decode_access_token_rejects_tampered_signature() -> None:
    """Verify that modifying the token signature raises TokenInvalidError."""
    user_id = uuid.uuid4()
    token = create_access_token(subject=user_id)

    # Tamper with the signature (last segment)
    parts = token.split(".")
    tampered_sig = parts[2][:-4] + "AAAA"
    tampered_token = f"{parts[0]}.{parts[1]}.{tampered_sig}"

    with pytest.raises(TokenInvalidError):
        decode_access_token(tampered_token)


def test_decode_access_token_rejects_tampered_payload() -> None:
    """Verify that tampering with the payload body raises TokenInvalidError."""
    user_id = uuid.uuid4()
    token = create_access_token(subject=user_id)

    # Replace payload segment
    parts = token.split(".")
    fake_payload_b64 = "eyJzdWIiOiAiaGFja2VyIn0"  # {"sub": "hacker"}
    tampered_token = f"{parts[0]}.{fake_payload_b64}.{parts[2]}"

    with pytest.raises(TokenInvalidError):
        decode_access_token(tampered_token)


def test_decode_access_token_rejects_wrong_secret_key() -> None:
    """Verify that token signed with a different key is rejected."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        "iss": get_settings().JWT_ISSUER,
        "aud": get_settings().JWT_AUDIENCE,
        "type": "access",
    }
    wrong_key_token = jwt.encode(
        payload, "wrong_secret_key_different_from_settings", algorithm="HS256"
    )

    with pytest.raises(TokenInvalidError):
        decode_access_token(wrong_key_token)


def test_decode_access_token_rejects_algorithm_confusion_none() -> None:
    """Verify that tokens signed with the 'none' algorithm are strictly rejected."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        "iss": get_settings().JWT_ISSUER,
        "aud": get_settings().JWT_AUDIENCE,
        "type": "access",
    }
    none_token = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(TokenInvalidError):
        decode_access_token(none_token)


def test_decode_access_token_rejects_wrong_token_type() -> None:
    """Verify that a token with type != 'access' is rejected."""
    settings = get_settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "refresh",  # Not access
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(TokenInvalidError) as exc_info:
        decode_access_token(token)
    assert "invalid token type" in str(exc_info.value).lower()


def test_decode_access_token_rejects_missing_required_claims() -> None:
    """Verify that tokens missing mandatory claims (e.g. sub, exp) fail verification."""
    settings = get_settings()
    # Missing 'sub'
    payload_no_sub = {
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "access",
    }
    token = jwt.encode(payload_no_sub, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with pytest.raises(TokenInvalidError):
        decode_access_token(token)


@pytest.mark.parametrize(
    "malformed_token",
    [
        "",
        "   ",
        "random_unencoded_string",
        "header.payload",  # Missing signature
        "part1.part2.part3.part4",  # Too many segments
    ],
)
def test_decode_access_token_rejects_malformed_strings(malformed_token: str) -> None:
    """Verify that arbitrary malformed strings are rejected with TokenInvalidError."""
    with pytest.raises(TokenInvalidError):
        decode_access_token(malformed_token)


def test_verify_dummy_password_constant_time() -> None:
    """Verify that dummy password verification runs safely without exception."""
    verify_dummy_password("test_candidate_password")
    verify_dummy_password("")
    verify_dummy_password("super_long_candidate_password_" * 10)


def test_create_refresh_token_structure_and_claims() -> None:
    """Verify refresh token contains type='refresh', 7-day expiration, and mandatory claims."""
    settings = get_settings()
    user_id = uuid.uuid4()
    before_ts = int(datetime.now(UTC).timestamp())

    token = create_refresh_token(subject=user_id)
    after_ts = int(datetime.now(UTC).timestamp())

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == str(user_id)
    assert claims["type"] == "refresh"
    assert claims["iss"] == settings.JWT_ISSUER
    assert claims["aud"] == settings.JWT_AUDIENCE
    assert "jti" in claims
    assert len(claims["jti"]) == 32

    assert before_ts <= claims["iat"] <= after_ts
    expected_exp = claims["iat"] + (settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 86400)
    assert claims["exp"] == expected_exp


def test_create_refresh_token_custom_expires_delta() -> None:
    """Verify refresh token supports explicit custom expiration delta."""
    user_id = uuid.uuid4()
    delta = timedelta(days=3)
    token = create_refresh_token(subject=user_id, expires_delta=delta)
    claims = decode_refresh_token(token)

    expected_duration = int(delta.total_seconds())
    actual_duration = claims["exp"] - claims["iat"]
    assert actual_duration == expected_duration


def test_decode_refresh_token_success() -> None:
    """Verify decode_refresh_token verifies a valid refresh token."""
    user_id = uuid.uuid4()
    token = create_refresh_token(subject=user_id)
    claims = decode_refresh_token(token)

    assert claims["sub"] == str(user_id)
    assert claims["type"] == "refresh"


def test_decode_refresh_token_rejects_expired() -> None:
    """Verify decode_refresh_token rejects an expired refresh token."""
    user_id = uuid.uuid4()
    expired = create_refresh_token(subject=user_id, expires_delta=timedelta(seconds=-10))

    with pytest.raises(TokenExpiredError):
        decode_refresh_token(expired)


def test_decode_refresh_token_rejects_tampered_signature() -> None:
    """Verify tampered refresh token signature raises TokenInvalidError."""
    user_id = uuid.uuid4()
    token = create_refresh_token(subject=user_id)
    parts = token.split(".")
    tampered = f"{parts[0]}.{parts[1]}.{parts[2][:-4]}XXXX"

    with pytest.raises(TokenInvalidError):
        decode_refresh_token(tampered)


def test_decode_refresh_token_rejects_tampered_payload() -> None:
    """Verify tampered refresh token payload raises TokenInvalidError."""
    user_id = uuid.uuid4()
    token = create_refresh_token(subject=user_id)
    parts = token.split(".")
    tampered = f"{parts[0]}.eyJzdWIiOiAiaGFjayJ9.{parts[2]}"

    with pytest.raises(TokenInvalidError):
        decode_refresh_token(tampered)


def test_decode_refresh_token_rejects_wrong_secret() -> None:
    """Verify refresh token signed with wrong secret is rejected."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
        "iss": get_settings().JWT_ISSUER,
        "aud": get_settings().JWT_AUDIENCE,
        "type": "refresh",
    }
    wrong_token = jwt.encode(payload, "different_secret_key_1234567890123456", algorithm="HS256")

    with pytest.raises(TokenInvalidError):
        decode_refresh_token(wrong_token)


def test_decode_refresh_token_rejects_none_algorithm() -> None:
    """Verify refresh token using 'none' algorithm is rejected."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
        "iss": get_settings().JWT_ISSUER,
        "aud": get_settings().JWT_AUDIENCE,
        "type": "refresh",
    }
    none_token = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(TokenInvalidError):
        decode_refresh_token(none_token)


def test_decode_refresh_token_rejects_wrong_issuer_and_audience() -> None:
    """Verify refresh token with wrong issuer or audience fails verification."""
    settings = get_settings()
    user_id = str(uuid.uuid4())
    now_ts = int(datetime.now(UTC).timestamp())

    # Wrong issuer
    bad_iss = jwt.encode(
        {
            "sub": user_id,
            "iat": now_ts,
            "exp": now_ts + 3600,
            "jti": uuid.uuid4().hex,
            "iss": "wrong",
            "aud": settings.JWT_AUDIENCE,
            "type": "refresh",
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(TokenInvalidError):
        decode_refresh_token(bad_iss)

    # Wrong audience
    bad_aud = jwt.encode(
        {
            "sub": user_id,
            "iat": now_ts,
            "exp": now_ts + 3600,
            "jti": uuid.uuid4().hex,
            "iss": settings.JWT_ISSUER,
            "aud": "wrong",
            "type": "refresh",
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(TokenInvalidError):
        decode_refresh_token(bad_aud)


def test_decode_refresh_token_rejects_non_uuid_subject() -> None:
    """Verify refresh token with non-UUID subject fails validation."""
    settings = get_settings()
    now_ts = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {
            "sub": "not-a-valid-uuid",
            "iat": now_ts,
            "exp": now_ts + 3600,
            "jti": uuid.uuid4().hex,
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "type": "refresh",
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(TokenInvalidError) as exc_info:
        decode_refresh_token(token)
    assert "uuid" in str(exc_info.value).lower()


def test_token_type_separation_access_vs_refresh() -> None:
    """Cross-type rejection: access token cannot be decoded as refresh, and vice versa."""
    user_id = uuid.uuid4()
    access_token = create_access_token(subject=user_id)
    refresh_token = create_refresh_token(subject=user_id)

    # Access token passed to decode_refresh_token MUST be rejected
    with pytest.raises(TokenInvalidError) as exc1:
        decode_refresh_token(access_token)
    assert "expected refresh token" in str(exc1.value).lower()

    # Refresh token passed to decode_access_token MUST be rejected
    with pytest.raises(TokenInvalidError) as exc2:
        decode_access_token(refresh_token)
    assert "expected access token" in str(exc2.value).lower()


def test_access_token_auth_version_claim() -> None:
    """Verify that create_access_token properly embeds auth_version and decode preserves it."""
    user_id = uuid.uuid4()

    # Default auth_version
    token_v0 = create_access_token(subject=user_id)
    payload_v0 = decode_access_token(token_v0)
    assert payload_v0.get("auth_version") == 0

    # Custom auth_version
    token_v3 = create_access_token(subject=user_id, auth_version=3)
    payload_v3 = decode_access_token(token_v3)
    assert payload_v3.get("auth_version") == 3
