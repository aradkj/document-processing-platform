"""Integration tests for Password Reset system against live PostgreSQL."""

import asyncio
import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import hash_password_reset_token
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User
from app.db.session import get_db_session
from app.services.email import EmailService, set_email_service
from tests.integration.test_database import is_postgres_available


@pytest.fixture(autouse=True)
def setup_mock_email_service() -> Generator[EmailService, None, None]:
    """Ensure mock email transport is used during tests to prevent real network calls."""
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)
    yield mock_svc
    set_email_service(None)


def _extract_reset_token(mock_svc: EmailService) -> str:
    """Extract raw password reset token from recorded email messages."""
    assert len(mock_svc.sent_messages) > 0, "No email was recorded by mock service"
    msg = mock_svc.sent_messages[-1]
    assert msg.get("type") == "password_reset"
    reset_url = str(msg.get("reset_url", ""))
    match = re.search(r"#token=([A-Za-z0-9_\-]+)", reset_url)
    assert match is not None, f"Could not find #token= in reset_url: {reset_url}"
    return match.group(1)


async def _create_test_user(
    async_client: AsyncClient,
    prefix: str,
    *,
    is_verified: bool = True,
    is_active: bool = True,
) -> tuple[dict[str, str], str, str]:
    """Register and log in a test user. Returns (user_info, access_token, refresh_token)."""
    uid = uuid.uuid4().hex[:8]
    email = f"{prefix}_{uid}@example.com"
    username = f"{prefix}_{uid}"
    password = f"SecurePass_{uid}!123"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = reg_resp.json()["id"]

    # Adjust user flags in DB if needed
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, uuid.UUID(user_id))
        assert user is not None
        user.is_verified = is_verified
        user.is_active = is_active
        await session.commit()
    finally:
        await session.close()

    # Log in only if active
    access_token = ""
    refresh_token = ""
    if is_active:
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200
        access_token = login_resp.json()["access_token"]
        refresh_token = login_resp.json()["refresh_token"]

    user_info = {
        "id": user_id,
        "email": email,
        "username": username,
        "password": password,
    }
    return user_info, access_token, refresh_token


@pytest.mark.asyncio
async def test_password_reset_request_existing_user(
    async_client: AsyncClient,
) -> None:
    """Verify password reset request for existing user dispatches email and stores hashed token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwreq")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    resp = await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    assert resp.status_code == 200
    assert (
        resp.json()["message"]
        == "If an account exists for this email, password reset instructions will be sent."
    )

    # Verify email was dispatched
    assert len(mock_svc.sent_messages) == 1
    raw_token = _extract_reset_token(mock_svc)
    expected_hash = hash_password_reset_token(raw_token)

    # Verify DB token record
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.user_id == uuid.UUID(user_info["id"])
        )
        res = await session.execute(stmt)
        token_record = res.scalar_one_or_none()
        assert token_record is not None
        assert token_record.token_hash == expected_hash
        assert token_record.used_at is None
        assert token_record.invalidated_at is None
        assert token_record.email == user_info["email"]
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_password_reset_request_nonexistent_and_inactive_users(
    async_client: AsyncClient,
) -> None:
    """Verify anti-enumeration: nonexistent and inactive users return identical response."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    # 1. Nonexistent user
    resp_none = await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "nonexistent_reset_user@example.com"},
    )
    assert resp_none.status_code == 200
    assert (
        resp_none.json()["message"]
        == "If an account exists for this email, password reset instructions will be sent."
    )
    assert len(mock_svc.sent_messages) == 0

    # 2. Inactive user
    user_info, _, _ = await _create_test_user(async_client, "inact_pw", is_active=False)
    mock_svc.sent_messages.clear()
    resp_inact = await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    assert resp_inact.status_code == 200
    assert (
        resp_inact.json()["message"]
        == "If an account exists for this email, password reset instructions will be sent."
    )
    assert len(mock_svc.sent_messages) == 0


@pytest.mark.asyncio
async def test_password_reset_confirm_successful_lifecycle(
    async_client: AsyncClient,
) -> None:
    """Verify password reset lifecycle including session invalidation and auth_version increment."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, old_access_token, old_refresh_token = await _create_test_user(async_client, "pwsucc")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    # Verify initial access token works at /me
    me_resp_before = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {old_access_token}"},
    )
    assert me_resp_before.status_code == 200

    # Request reset
    req_resp = await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    assert req_resp.status_code == 200
    raw_token = _extract_reset_token(mock_svc)

    # Confirm reset with new password
    new_password = "BrandNewSuperSecurePass2026!"
    confirm_resp = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": new_password},
    )
    assert confirm_resp.status_code == 200
    assert "reset successfully" in confirm_resp.json()["message"].lower()

    # 1. Verify previous access token is immediately rejected at /me (due to auth_version mismatch)
    me_resp_after = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {old_access_token}"},
    )
    assert me_resp_after.status_code == 401

    # 2. Verify previous refresh token is rejected
    refresh_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh_token},
    )
    assert refresh_resp.status_code == 401

    # 3. Verify old password cannot log in
    old_login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": user_info["email"], "password": user_info["password"]},
    )
    assert old_login.status_code == 401

    # 4. Verify new password successfully logs in
    new_login = await async_client.post(
        "/api/v1/auth/login",
        json={"email": user_info["email"], "password": new_password},
    )
    assert new_login.status_code == 200
    new_access_token = new_login.json()["access_token"]

    # 5. Verify new access token works at /me
    me_new = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {new_access_token}"},
    )
    assert me_new.status_code == 200
    assert me_new.json()["email"] == user_info["email"]

    # 6. Verify DB state: auth_version incremented, token consumed, refresh families revoked
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, uuid.UUID(user_info["id"]))
        assert user is not None
        assert user.auth_version == 1

        # Check old refresh token was revoked
        rt_old_stmt = select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_not(None)
        )
        rt_old_res = await session.execute(rt_old_stmt)
        revoked_tokens = list(rt_old_res.scalars())
        assert len(revoked_tokens) >= 1

        # Check new refresh token is active
        rt_new_stmt = select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
        rt_new_res = await session.execute(rt_new_stmt)
        active_tokens = list(rt_new_res.scalars())
        assert len(active_tokens) == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_password_reset_token_reuse_rejected(
    async_client: AsyncClient,
) -> None:
    """Verify consumed password reset token cannot be reused."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwreuse")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    raw_token = _extract_reset_token(mock_svc)

    # First consumption succeeds
    resp1 = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "NewValidPassword_123!"},
    )
    assert resp1.status_code == 200

    # Second consumption fails
    resp2 = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "AnotherValidPassword_123!"},
    )
    assert resp2.status_code == 400
    assert "invalid or expired" in resp2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_token_expired_rejected(
    async_client: AsyncClient,
) -> None:
    """Verify expired password reset token is rejected."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwexp")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    raw_token = _extract_reset_token(mock_svc)

    # Manually expire the token in the database
    token_hash = hash_password_reset_token(raw_token)
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        stmt = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        res = await session.execute(stmt)
        record = res.scalar_one_or_none()
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()
    finally:
        await session.close()

    # Attempt confirm
    resp = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "ValidPassword123!"},
    )
    assert resp.status_code == 400
    assert "invalid or expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_new_request_invalidates_prior_tokens(
    async_client: AsyncClient,
) -> None:
    """Verify issuing a second reset request invalidates the first token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwinval")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    # First request
    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    token1 = _extract_reset_token(mock_svc)

    # Second request
    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    token2 = _extract_reset_token(mock_svc)
    assert token1 != token2

    # Attempt to use first token (should fail as invalidated)
    resp1 = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token1, "new_password": "NewPassword123!"},
    )
    assert resp1.status_code == 400
    assert "invalid or expired" in resp1.json()["detail"].lower()

    # Use second token (should succeed)
    resp2 = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token2, "new_password": "NewPassword123!"},
    )
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_email_mismatch_rejected(
    async_client: AsyncClient,
) -> None:
    """Verify reset token issued for an old email cannot be used after email change."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, access_token, _ = await _create_test_user(async_client, "pwchg")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    # Request reset with current email
    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    raw_token = _extract_reset_token(mock_svc)

    # User changes their email via profile update
    new_email = f"changed_{uuid.uuid4().hex[:6]}@example.com"
    patch_resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": new_email},
    )
    assert patch_resp.status_code == 200

    # Attempt to reset password using token issued for old email
    confirm_resp = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "NewPassword123!"},
    )
    assert confirm_resp.status_code == 400
    assert "invalid or expired" in confirm_resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_password_reset_weak_password_rejected(
    async_client: AsyncClient,
) -> None:
    """Verify weak password is rejected with 422 and does not consume the token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwweak")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    raw_token = _extract_reset_token(mock_svc)

    # Weak password (fails policy)
    weak_resp = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "weak"},
    )
    assert weak_resp.status_code == 422

    # Token remains valid for a good password
    strong_resp = await async_client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": raw_token, "new_password": "StrongPassword2026!"},
    )
    assert strong_resp.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_concurrency_simultaneous_requests(
    async_client: AsyncClient,
) -> None:
    """Verify concurrent reset requests produce exactly one active token in PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwcreq")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    # Trigger 5 concurrent reset requests for the same user
    async def make_request() -> int:
        r = await async_client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": user_info["email"]},
        )
        return r.status_code

    results = await asyncio.gather(*(make_request() for _ in range(5)))
    assert all(code == 200 for code in results)

    # Verify database state: exactly 1 token is active (not invalidated, not used)
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        stmt = select(PasswordResetToken).where(
            PasswordResetToken.user_id == uuid.UUID(user_info["id"]),
            PasswordResetToken.invalidated_at.is_(None),
            PasswordResetToken.used_at.is_(None),
        )
        res = await session.execute(stmt)
        active_tokens = list(res.scalars())
        assert len(active_tokens) == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_password_reset_concurrency_simultaneous_confirm(
    async_client: AsyncClient,
) -> None:
    """Verify concurrent confirmation attempts with the same token allow exactly one to succeed."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _, _ = await _create_test_user(async_client, "pwconf")
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)

    await async_client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": user_info["email"]},
    )
    raw_token = _extract_reset_token(mock_svc)

    # 3 concurrent confirmation calls with the exact same token
    async def confirm_call(idx: int) -> int:
        r = await async_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": raw_token, "new_password": f"PasswordVariant_{idx}!99"},
        )
        return r.status_code

    results = await asyncio.gather(*(confirm_call(i) for i in range(3)))
    success_count = sum(1 for code in results if code == 200)
    failed_count = sum(1 for code in results if code == 400)

    # Exactly one succeeded, the others failed
    assert success_count == 1
    assert failed_count == 2

    # Check user auth_version incremented exactly once (from 0 to 1)
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, uuid.UUID(user_info["id"]))
        assert user is not None
        assert user.auth_version == 1
    finally:
        await session.close()
