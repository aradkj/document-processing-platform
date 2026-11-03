"""Integration tests for Profile API (GET /me, PATCH /me) against live PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token
from app.db.models.user import User
from app.db.session import get_db_session
from tests.integration.test_database import is_postgres_available


async def _create_test_user(
    async_client: AsyncClient,
    prefix: str,
    *,
    is_verified: bool = False,
    is_active: bool = True,
) -> tuple[dict[str, str], str]:
    """Helper to register a user, set active/verified flags, and return user info + access token."""
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

    # Optionally adjust is_verified / is_active directly in DB
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
    return user_info, access_token


@pytest.mark.asyncio
async def test_get_me_success_and_response_safety(async_client: AsyncClient) -> None:
    """Verify GET /me returns safe profile attributes with valid Bearer token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "get_me_success")

    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["id"] == user_info["id"]
    assert data["email"] == user_info["email"]
    assert data["username"] == user_info["username"]
    assert data["is_active"] is True
    assert "created_at" in data
    assert "updated_at" in data

    # Verify no sensitive fields leaked
    sensitive_keys = {
        "password",
        "password_hash",
        "refresh_token",
        "access_token",
        "jti",
        "family_id",
    }
    for key in sensitive_keys:
        assert key not in data


@pytest.mark.asyncio
async def test_get_me_unauthenticated_or_invalid_tokens(async_client: AsyncClient) -> None:
    """Verify GET /me fails safely without token, with expired token, or with invalid token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # 1. Missing token
    resp = await async_client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers

    # 2. Malformed token
    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token.value"},
    )
    assert resp.status_code == 401

    # 3. Expired token
    user_id = uuid.uuid4()
    expired_token = create_access_token(
        subject=user_id,
        expires_delta=timedelta(seconds=-10),
    )
    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401
    assert "Token has expired" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_me_inactive_or_deleted_user(async_client: AsyncClient) -> None:
    """Verify GET /me rejects inactive or deleted users even with valid JWT signature."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "inactive_get_me")

    # Deactivate user in DB
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = await session.get(User, uuid.UUID(user_info["id"]))
        assert user is not None
        user.is_active = False
        await session.commit()
    finally:
        await session.close()

    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert "inactive" in resp.json()["detail"].lower()

    # Delete user from DB
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    try:
        user2 = await session2.get(User, uuid.UUID(user_info["id"]))
        if user2 is not None:
            await session2.delete(user2)
            await session2.commit()
    finally:
        await session2.close()

    resp2 = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 401
    assert "User not found" in resp2.json()["detail"]


@pytest.mark.asyncio
async def test_patch_me_update_username_only(async_client: AsyncClient) -> None:
    """Verify PATCH /me can update username only while preserving other attributes."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "patch_user_only", is_verified=True)
    new_username = f"new_name_{uuid.uuid4().hex[:6]}"

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": new_username},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == new_username
    assert data["email"] == user_info["email"]
    # Username change should NOT reset is_verified
    assert data["is_verified"] is True


@pytest.mark.asyncio
async def test_patch_me_update_email_resets_is_verified(async_client: AsyncClient) -> None:
    """Verify updating email normalizes case and resets is_verified to False."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "patch_email_reset", is_verified=True)
    raw_new_email = f"  NEW_Email_{uuid.uuid4().hex[:6]}@EXAMPLE.COM  "
    expected_normalized = raw_new_email.strip().lower()

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": raw_new_email},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == expected_normalized
    assert data["username"] == user_info["username"]
    # Security requirement: email change must set is_verified to False
    assert data["is_verified"] is False


@pytest.mark.asyncio
async def test_patch_me_update_both_fields(async_client: AsyncClient) -> None:
    """Verify PATCH /me updates both email and username simultaneously."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, token = await _create_test_user(async_client, "patch_both")
    new_email = f"both_{uuid.uuid4().hex[:6]}@example.com"
    new_username = f"both_{uuid.uuid4().hex[:6]}"

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": new_email, "username": new_username},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == new_email
    assert data["username"] == new_username


@pytest.mark.asyncio
async def test_patch_me_empty_update_is_safe_noop(async_client: AsyncClient) -> None:
    """Verify empty PATCH /me payload returns current profile without error."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "patch_empty")

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == user_info["email"]
    assert data["username"] == user_info["username"]


@pytest.mark.asyncio
async def test_patch_me_same_values_succeeds(async_client: AsyncClient) -> None:
    """Verify submitting current email and username succeeds (no self-conflict)."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "patch_same")

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": user_info["email"], "username": user_info["username"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == user_info["email"]
    assert data["username"] == user_info["username"]


@pytest.mark.asyncio
async def test_patch_me_duplicate_email_conflict(async_client: AsyncClient) -> None:
    """Verify attempting to update email to an existing user's email returns 409 Conflict."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user1_info, _ = await _create_test_user(async_client, "user_one")
    _user2_info, token2 = await _create_test_user(async_client, "user_two")

    # User 2 attempts to change email to User 1's email (case-insensitive)
    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token2}"},
        json={"email": user1_info["email"].upper()},
    )
    assert resp.status_code == 409
    err = resp.json()["detail"]
    assert "email already exists" in err.lower()
    # Confirm no user enumeration leak
    assert user1_info["username"] not in err
    assert user1_info["id"] not in err


@pytest.mark.asyncio
async def test_patch_me_duplicate_username_conflict(async_client: AsyncClient) -> None:
    """Verify attempting to update username to an existing user's username returns 409 Conflict."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user1_info, _ = await _create_test_user(async_client, "target_user")
    _user2_info, token2 = await _create_test_user(async_client, "competing_user")

    # User 2 attempts to change username to User 1's username (case-insensitive)
    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token2}"},
        json={"username": user1_info["username"].upper()},
    )
    assert resp.status_code == 409
    err = resp.json()["detail"]
    assert "username already exists" in err.lower()
    # Confirm no user enumeration leak
    assert user1_info["email"] not in err
    assert user1_info["id"] not in err


@pytest.mark.asyncio
async def test_patch_me_forbidden_fields_strictly_rejected(
    async_client: AsyncClient,
) -> None:
    """Verify client cannot tamper with sensitive or immutable fields via PATCH /me."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, token = await _create_test_user(async_client, "forbidden_patch")

    forbidden_payloads = [
        {"is_active": False},
        {"is_verified": True},
        {"password_hash": "evil_hash"},
        {"roles": ["admin"]},
        {"id": str(uuid.uuid4())},
        {"user_id": str(uuid.uuid4())},
        {"created_at": datetime.now(UTC).isoformat()},
        {"updated_at": datetime.now(UTC).isoformat()},
        {"permissions": ["users:delete"]},
    ]

    for payload in forbidden_payloads:
        resp = await async_client.patch(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert resp.status_code == 422, f"Failed for payload: {payload}"


@pytest.mark.asyncio
async def test_patch_me_invalid_field_formats(async_client: AsyncClient) -> None:
    """Verify validation errors for bad email or username formats."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    _user_info, token = await _create_test_user(async_client, "invalid_fmt")

    # Invalid email
    resp1 = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": "not-a-valid-email"},
    )
    assert resp1.status_code == 422

    # Invalid username (too short)
    resp2 = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "ab"},
    )
    assert resp2.status_code == 422

    # Invalid username (invalid characters)
    resp3 = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "invalid user!"},
    )
    assert resp3.status_code == 422


@pytest.mark.asyncio
async def test_patch_me_explicit_null_fields_rejected_and_db_unmodified(
    async_client: AsyncClient,
) -> None:
    """Verify explicit nulls for email/username return 422 and leave database unmodified."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "null_test_user")
    initial_email = user_info["email"]
    initial_username = user_info["username"]
    user_uuid = uuid.UUID(user_info["id"])

    # 1. Explicit email: null -> 422
    resp_null_email = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": None},
    )
    assert resp_null_email.status_code == 422
    err_fields = [e["loc"][-1] for e in resp_null_email.json()["detail"]]
    assert "email" in err_fields

    # Verify DB record is untouched
    session_gen1 = get_db_session()
    session1 = await anext(session_gen1)
    try:
        user_db = await session1.get(User, user_uuid)
        assert user_db is not None
        assert user_db.email == initial_email
        assert user_db.username == initial_username
    finally:
        await session1.close()

    # 2. Explicit username: null -> 422
    resp_null_user = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": None},
    )
    assert resp_null_user.status_code == 422
    err_fields = [e["loc"][-1] for e in resp_null_user.json()["detail"]]
    assert "username" in err_fields

    # Verify DB record is untouched
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    try:
        user_db = await session2.get(User, user_uuid)
        assert user_db is not None
        assert user_db.email == initial_email
        assert user_db.username == initial_username
    finally:
        await session2.close()

    # 3. Explicit both: null -> 422
    resp_null_both = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": None, "username": None},
    )
    assert resp_null_both.status_code == 422
    err_fields = [e["loc"][-1] for e in resp_null_both.json()["detail"]]
    assert "email" in err_fields
    assert "username" in err_fields

    # Verify DB record is untouched
    session_gen3 = get_db_session()
    session3 = await anext(session_gen3)
    try:
        user_db = await session3.get(User, user_uuid)
        assert user_db is not None
        assert user_db.email == initial_email
        assert user_db.username == initial_username
    finally:
        await session3.close()

    # 4. Empty payload {} -> 200 safe no-op
    resp_empty = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp_empty.status_code == 200
    assert resp_empty.json()["email"] == initial_email
    assert resp_empty.json()["username"] == initial_username

    # 5. Omitted email with valid username -> updates username only, email unchanged
    new_uname = f"updated_uname_{uuid.uuid4().hex[:6]}"
    resp_omitted_email = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": new_uname},
    )
    assert resp_omitted_email.status_code == 200
    assert resp_omitted_email.json()["email"] == initial_email
    assert resp_omitted_email.json()["username"] == new_uname

    # 6. Omitted username with valid email -> updates email only, username unchanged
    new_mail = f"updated_mail_{uuid.uuid4().hex[:6]}@example.com"
    resp_omitted_user = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"email": new_mail},
    )
    assert resp_omitted_user.status_code == 200
    assert resp_omitted_user.json()["email"] == new_mail
    assert resp_omitted_user.json()["username"] == new_uname


@pytest.mark.asyncio
async def test_patch_me_authorization_and_user_isolation(async_client: AsyncClient) -> None:
    """Verify User A cannot update User B's profile and unauthenticated requests fail."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_a, token_a = await _create_test_user(async_client, "user_iso_a")
    user_b, token_b = await _create_test_user(async_client, "user_iso_b")

    # Unauthenticated request
    unauth_resp = await async_client.patch(
        "/api/v1/auth/me",
        json={"username": "attacker_attempt"},
    )
    assert unauth_resp.status_code == 401

    # User A updates profile; verify User B remains completely untouched
    new_username_a = f"updated_a_{uuid.uuid4().hex[:6]}"
    resp_a = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"username": new_username_a},
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["id"] == user_a["id"]
    assert resp_a.json()["username"] == new_username_a

    # Check User B's profile is still user_b
    resp_b = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["id"] == user_b["id"]
    assert resp_b.json()["username"] == user_b["username"]


@pytest.mark.asyncio
async def test_patch_me_persistence_and_updated_at(async_client: AsyncClient) -> None:
    """Verify changes persist in PostgreSQL and updated_at timestamp advances."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_info, token = await _create_test_user(async_client, "persist_test")
    new_username = f"persist_{uuid.uuid4().hex[:6]}"

    # Fetch initial updated_at
    initial_get = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert initial_get.status_code == 200
    initial_updated_at_str = initial_get.json()["updated_at"]

    resp = await async_client.patch(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": new_username},
    )
    assert resp.status_code == 200
    patch_data = resp.json()
    assert patch_data["username"] == new_username
    assert patch_data["updated_at"] is not None

    # Query directly from a fresh DB session
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user_db = await session.get(User, uuid.UUID(user_info["id"]))
        assert user_db is not None
        assert user_db.username == new_username
        if initial_updated_at_str is not None:
            initial_dt = datetime.fromisoformat(initial_updated_at_str)
            assert user_db.updated_at >= initial_dt
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_openapi_schema_contains_profile_patch_endpoint(
    async_client: AsyncClient,
) -> None:
    """Verify OpenAPI document contains PATCH /api/v1/auth/me with security and schemas."""
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    paths = spec["paths"]
    assert "/api/v1/auth/me" in paths
    assert "patch" in paths["/api/v1/auth/me"]

    patch_op = paths["/api/v1/auth/me"]["patch"]
    assert "200" in patch_op["responses"]
    assert "401" in patch_op["responses"]
    assert "409" in patch_op["responses"]
    assert "422" in patch_op["responses"]

    # Security requirement
    assert "security" in patch_op
    assert any("HTTPBearer" in req for req in patch_op["security"])

    components = spec.get("components", {}).get("schemas", {})
    assert "UserProfileUpdateRequest" in components
    assert "UserProfileResponse" in components
