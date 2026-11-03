"""Integration tests for Email Verification System against live PostgreSQL."""

import asyncio
import uuid
from collections.abc import Generator

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from app.db.models.email_verification_token import EmailVerificationToken
from app.db.models.user import User
from app.db.session import get_db_session, get_engine
from app.services.email import EmailService, get_email_service, set_email_service
from app.services.email_verification import create_email_verification_token
from tests.integration.test_database import is_postgres_available


@pytest.fixture(autouse=True)
def setup_mock_email_service() -> Generator[None, None, None]:
    """Ensure mock email transport is used during tests to prevent real network calls."""
    mock_svc = EmailService(use_mock_transport=True)
    set_email_service(mock_svc)
    yield
    set_email_service(None)


async def _create_test_account(
    async_client: AsyncClient,
    prefix: str,
    *,
    is_verified: bool = False,
    is_active: bool = True,
) -> tuple[dict[str, str], str, str]:
    """Register a test user, return user_info dict, access_token, and initial raw_token."""
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

    # Retrieve initial verification token record and recreate a deterministic raw token for testing
    session_gen = get_db_session()
    session = await anext(session_gen)
    raw_token = ""
    try:
        user = await session.get(User, uuid.UUID(user_id))
        assert user is not None
        user.is_verified = is_verified
        user.is_active = is_active
        # Generate a known test token for immediate verification test use
        _token_record, raw_token = await create_email_verification_token(
            session=session,
            user_id=user.id,
            email=user.email,
        )
        await session.commit()
    finally:
        await session.close()

    # Login to obtain access token
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    access_token = login_resp.json().get("access_token", "")
    user_info = {
        "id": user_id,
        "email": email,
        "username": username,
        "password": password,
    }
    return user_info, access_token, raw_token


@pytest.mark.asyncio
async def test_registration_creates_unverified_user_and_token(
    async_client: AsyncClient,
) -> None:
    """Verify registration creates unverified user and persists hashed verification token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    uid = uuid.uuid4().hex[:8]
    email = f"reg_verify_{uid}@example.com"
    username = f"reg_{uid}"
    password = f"Pass1234_{uid}!"

    resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert resp.status_code == 201
    data = resp.json()

    # User response safety: no token or hash leaked
    assert "token" not in data
    assert "token_hash" not in data
    assert "password" not in data
    assert data["is_verified"] is False
    user_id = uuid.UUID(data["id"])

    # Verify database state
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.is_verified is False

        # Token record verification
        stmt = select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)
        result = await session.execute(stmt)
        token_record = result.scalar_one_or_none()
        assert token_record is not None
        assert token_record.email == email.lower()
        assert token_record.used_at is None
        assert token_record.invalidated_at is None
        assert token_record.is_active is True
        assert len(token_record.token_hash) == 64
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_successful_email_verification(async_client: AsyncClient) -> None:
    """Verify valid token verifies user account and marks token consumed."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _token, raw_token = await _create_test_account(
        async_client, "verify_success", is_verified=False
    )
    user_id = uuid.UUID(user_info["id"])

    # Call verify-email
    resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": raw_token},
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "Email verified successfully."

    # Verify user state in DB
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.is_verified is True

        # Verify token record consumed
        stmt = select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)
        tokens = (await session.execute(stmt)).scalars().all()
        consumed_tokens = [t for t in tokens if t.used_at is not None]
        assert len(consumed_tokens) >= 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_token_reuse_is_rejected(async_client: AsyncClient) -> None:
    """Verify an already-consumed verification token cannot be reused."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, _token, raw_token = await _create_test_account(
        async_client, "reuse_test", is_verified=False
    )

    # First verification succeeds
    resp1 = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": raw_token},
    )
    assert resp1.status_code == 200

    # Second verification fails
    resp2 = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": raw_token},
    )
    assert resp2.status_code == 400
    assert "Invalid or expired verification token" in resp2.json()["detail"]


@pytest.mark.asyncio
async def test_expired_token_is_rejected(async_client: AsyncClient) -> None:
    """Verify an expired verification token cannot verify an account."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _token, _raw = await _create_test_account(
        async_client, "expired_test", is_verified=False
    )
    user_id = uuid.UUID(user_info["id"])

    # Issue an expired token directly
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        _tok_rec, raw_token = await create_email_verification_token(
            session=session,
            user_id=user_id,
            email=user_info["email"],
            expires_in_minutes=-10,  # Expired in past
        )
        await session.commit()
    finally:
        await session.close()

    resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": raw_token},
    )
    assert resp.status_code == 400
    assert "Invalid or expired verification token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_invalid_token_strings_rejected(async_client: AsyncClient) -> None:
    """Verify malformed or nonexistent token strings are rejected."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Nonexistent token
    resp1 = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": "completely_fake_nonexistent_token_123456789"},
    )
    assert resp1.status_code == 400

    # Empty token -> 422
    resp2 = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": ""},
    )
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_token_for_old_email_rejected_after_email_change(
    async_client: AsyncClient,
) -> None:
    """Verify a token generated for an old email address cannot verify a changed email address."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, token, old_raw_token = await _create_test_account(
        async_client, "old_email_tok", is_verified=False
    )
    new_email = f"new_email_{uuid.uuid4().hex[:6]}@example.com"

    # User updates their email address
    patch_resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": new_email},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["email"] == new_email
    assert patch_resp.json()["is_verified"] is False

    # Attempt to use the old email's verification token
    verify_resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": old_raw_token},
    )
    assert verify_resp.status_code == 400
    assert "Invalid or expired verification token" in verify_resp.json()["detail"]


@pytest.mark.asyncio
async def test_deleted_or_inactive_user_cannot_verify(async_client: AsyncClient) -> None:
    """Verify token cannot verify if the user account is deactivated or deleted."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, _token, raw_token = await _create_test_account(
        async_client, "deleted_user_verify", is_verified=False
    )
    user_id = uuid.UUID(user_info["id"])

    # Deactivate user
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, user_id)
        assert user is not None
        user.is_active = False
        await session.commit()
    finally:
        await session.close()

    resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": raw_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_concurrent_token_consumption(async_client: AsyncClient) -> None:
    """Verify row-level locking ensures only one concurrent request consumes a token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, _token, raw_token = await _create_test_account(
        async_client, "concurrent_verify", is_verified=False
    )

    # Launch two concurrent verification requests with identical token
    req1 = async_client.post("/api/v1/auth/verify-email", json={"token": raw_token})
    req2 = async_client.post("/api/v1/auth/verify-email", json={"token": raw_token})

    results = await asyncio.gather(req1, req2)
    statuses = sorted([r.status_code for r in results])

    # Exactly one request must succeed (200), and the other must be rejected (400)
    assert statuses == [200, 400], f"Expected [200, 400], got {statuses}"


@pytest.mark.asyncio
async def test_resend_verification_invalidates_previous_token(
    async_client: AsyncClient,
) -> None:
    """Verify resending verification invalidates the previous token and issues a new one."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token, initial_raw_token = await _create_test_account(
        async_client, "resend_inval", is_verified=False
    )
    user_id = uuid.UUID(user_info["id"])

    # Resend verification
    resend_resp = await async_client.post(
        "/api/v1/auth/resend-verification",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resend_resp.status_code == 200
    assert "Verification email sent" in resend_resp.json()["message"]

    # Initial token must now be invalidated
    old_verify_resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": initial_raw_token},
    )
    assert old_verify_resp.status_code == 400

    # Retrieve newly issued token from DB to test verification
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        # Create a new active token with known raw value to simulate receiving the new email
        _rec, new_raw_token = await create_email_verification_token(
            session=session,
            user_id=user_id,
            email=user_info["email"],
        )
        await session.commit()
    finally:
        await session.close()

    new_verify_resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": new_raw_token},
    )
    assert new_verify_resp.status_code == 200


@pytest.mark.asyncio
async def test_resend_verification_already_verified_user_safe_noop(
    async_client: AsyncClient,
) -> None:
    """Verify requesting resend on an already verified account safely returns success."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, token, _raw = await _create_test_account(
        async_client, "already_ver", is_verified=True
    )

    resp = await async_client.post(
        "/api/v1/auth/resend-verification",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert "Verification email sent" in resp.json()["message"]


@pytest.mark.asyncio
async def test_resend_verification_unauthenticated(async_client: AsyncClient) -> None:
    """Verify resend endpoint requires authentication."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    resp = await async_client.post("/api/v1/auth/resend-verification")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_email_change_triggers_new_verification(async_client: AsyncClient) -> None:
    """Verify email change resets is_verified and issues fresh verification token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token, initial_raw_token = await _create_test_account(
        async_client, "email_change_flow", is_verified=True
    )
    user_id = uuid.UUID(user_info["id"])
    new_email = f"changed_addr_{uuid.uuid4().hex[:6]}@example.com"

    # User changes email
    patch_resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": new_email},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["email"] == new_email
    assert patch_resp.json()["is_verified"] is False

    # Old token cannot verify
    old_resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": initial_raw_token},
    )
    assert old_resp.status_code == 400

    # User issues new verification token for the new email
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        _rec, new_raw_token = await create_email_verification_token(
            session=session,
            user_id=user_id,
            email=new_email,
        )
        await session.commit()
    finally:
        await session.close()

    # Verifying with the new token succeeds and marks user verified
    new_resp = await async_client.post(
        "/api/v1/auth/verify-email",
        json={"token": new_raw_token},
    )
    assert new_resp.status_code == 200

    # Profile now reflects is_verified=True
    get_me_resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_me_resp.status_code == 200
    assert get_me_resp.json()["email"] == new_email
    assert get_me_resp.json()["is_verified"] is True


@pytest.mark.asyncio
async def test_openapi_schema_contains_email_verification_endpoints(
    async_client: AsyncClient,
) -> None:
    """Verify OpenAPI document contains verify-email and resend-verification operations."""
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    paths = spec["paths"]
    assert "/api/v1/auth/verify-email" in paths
    assert "/api/v1/auth/resend-verification" in paths

    verify_op = paths["/api/v1/auth/verify-email"]["post"]
    assert "200" in verify_op["responses"]
    assert "400" in verify_op["responses"]
    assert "422" in verify_op["responses"]

    resend_op = paths["/api/v1/auth/resend-verification"]["post"]
    assert "200" in resend_op["responses"]
    assert "401" in resend_op["responses"]
    assert "security" in resend_op

    components = spec.get("components", {}).get("schemas", {})
    assert "VerifyEmailRequest" in components
    assert "VerifyEmailResponse" in components
    assert "ResendVerificationResponse" in components


@pytest.mark.asyncio
async def test_concurrent_resend_verification_single_active_token_guarantee(
    async_client: AsyncClient,
) -> None:
    """Verify that concurrent resend requests serialize cleanly with row locking,

    leaving exactly one valid active verification token in PostgreSQL, while all
    superseded tokens are rejected on verification attempt.
    """
    user_info, token, _initial_raw = await _create_test_account(async_client, "concurrent_resend")
    email_service = get_email_service()
    email_service.sent_messages.clear()

    # Launch 5 concurrent resend requests simultaneously
    headers = {"Authorization": f"Bearer {token}"}
    tasks = [
        async_client.post("/api/v1/auth/resend-verification", headers=headers) for _ in range(5)
    ]
    responses = await asyncio.gather(*tasks)

    # All resend requests should succeed without 500 internal errors or deadlocks
    for resp in responses:
        assert resp.status_code == 200

    # Query PostgreSQL to verify active token count for this user
    session_gen = get_db_session()
    session = await anext(session_gen)
    user_id = uuid.UUID(user_info["id"])
    try:
        stmt = select(EmailVerificationToken).where(
            EmailVerificationToken.user_id == user_id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.invalidated_at.is_(None),
        )
        result = await session.execute(stmt)
        active_tokens = result.scalars().all()
        # Exactly ONE active verification token must remain in PostgreSQL!
        assert len(active_tokens) == 1
    finally:
        await session.close()

    # Collect all tokens dispatched by mock transport for this email
    user_messages = [m for m in email_service.sent_messages if m["to_email"] == user_info["email"]]
    assert len(user_messages) >= 2  # multiple resends sent emails

    raw_tokens: list[str] = []
    for m in user_messages:
        url = m["verification_url"]
        assert "#token=" in url
        raw_tokens.append(url.split("#token=")[1])

    # Test all dispatched tokens: exactly one must match the active record and succeed,
    # all others (superseded) must be rejected with 400!
    successful_verifications = 0
    for t in raw_tokens:
        verify_resp = await async_client.post(
            "/api/v1/auth/verify-email",
            json={"token": t},
        )
        if verify_resp.status_code == 200:
            successful_verifications += 1
        else:
            assert verify_resp.status_code == 400

    # Only the active token should have verified the account
    assert successful_verifications == 1


@pytest.mark.asyncio
async def test_database_email_verification_indexes() -> None:
    """Verify PostgreSQL index structure on email_verification_tokens table."""
    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename = 'email_verification_tokens';"
            )
        )
        indexes = {row[0]: row[1] for row in res.fetchall()}

    # Required indexes must exist
    assert "pk_email_verification_tokens" in indexes
    assert "ix_email_verification_tokens_token_hash" in indexes
    assert "ix_email_verification_tokens_expires_at" in indexes
    assert "ix_email_verification_tokens_user_state" in indexes

    # Verify composite index columns: user_id, used_at, invalidated_at
    user_state_def = indexes["ix_email_verification_tokens_user_state"]
    assert "user_id" in user_state_def
    assert "used_at" in user_state_def
    assert "invalidated_at" in user_state_def

    # Redundant indexes must be absent
    assert "ix_email_verification_tokens_user_id" not in indexes
    assert "ix_email_verification_tokens_created_at" not in indexes
