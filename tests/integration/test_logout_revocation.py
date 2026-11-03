"""Integration tests for persistent refresh token revocation, logout, and cascade semantics."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.future import select

from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token
from app.db.models.refresh_token import RefreshToken
from app.db.models.user import User
from app.db.session import get_session_factory
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_login_creates_persistent_session_in_postgres(async_client: AsyncClient) -> None:
    """Verify login transactionally creates a RefreshToken record in PostgreSQL matching JTI."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"session_{unique_id}@example.com"
    username = f"session_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # 1. Register & Login
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    refresh_token = login_resp.json()["refresh_token"]

    # Decode unverified to inspect claims
    payload = jwt.decode(refresh_token, options={"verify_signature": False})
    jti = payload["jti"]
    user_id = uuid.UUID(payload["sub"])

    # 2. Query PostgreSQL directly
    async with get_session_factory()() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        token_session = result.scalar_one_or_none()
        assert token_session is not None
        assert token_session.user_id == user_id
        assert token_session.jti == jti
        assert token_session.revoked_at is None
        assert token_session.expires_at is not None
        assert token_session.is_revoked is False
        assert token_session.is_active is True


@pytest.mark.asyncio
async def test_raw_refresh_token_never_stored_in_database(async_client: AsyncClient) -> None:
    """Verify raw JWT token string is never stored anywhere in PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"noraw_{unique_id}@example.com"
    username = f"noraw_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    raw_token = login_resp.json()["refresh_token"]

    # Query all text/varchar columns in refresh_tokens table for raw token or header prefix
    async with get_session_factory()() as session:
        # Search for exact raw token
        exact_check = await session.execute(
            text("SELECT COUNT(*) FROM refresh_tokens WHERE jti = :raw OR replaced_by_jti = :raw"),
            {"raw": raw_token},
        )
        assert exact_check.scalar() == 0

        # Search for JWT prefix
        jwt_prefix_check = await session.execute(
            text(
                "SELECT COUNT(*) FROM refresh_tokens "
                "WHERE jti LIKE 'eyJ%' OR replaced_by_jti LIKE 'eyJ%'"
            )
        )
        assert jwt_prefix_check.scalar() == 0


@pytest.mark.asyncio
async def test_refresh_requires_existing_persistent_session(async_client: AsyncClient) -> None:
    """Verify a valid JWT refresh token without a corresponding DB session returns 401."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"nosess_{unique_id}@example.com"
    username = f"nosess_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    user_id = reg_resp.json()["id"]

    # Create a cryptographically valid refresh token with an unpersisted JTI
    orphan_jti = uuid.uuid4().hex
    orphan_token = create_refresh_token(subject=user_id, jti=orphan_jti)

    # Attempt refresh
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": orphan_token},
    )
    assert ref_resp.status_code == 401
    assert ref_resp.headers.get("www-authenticate") == "Bearer"
    assert ref_resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_logout_revokes_session_and_prevents_refresh(async_client: AsyncClient) -> None:
    """Verify logout revokes session in PostgreSQL and prevents subsequent refresh."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"logout_{unique_id}@example.com"
    username = f"logout_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    refresh_token = login_resp.json()["refresh_token"]

    payload = jwt.decode(refresh_token, options={"verify_signature": False})
    jti = payload["jti"]

    # 1. Logout
    logout_resp = await async_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_resp.status_code == 200
    assert logout_resp.json()["message"] == "Successfully logged out."

    # 2. Verify directly in PostgreSQL that revoked_at is set
    async with get_session_factory()() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        token_session = result.scalar_one()
        assert token_session.revoked_at is not None
        assert token_session.is_revoked is True
        assert token_session.is_active is False

    # 3. Attempt to use revoked refresh token -> must return 401
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert ref_resp.status_code == 401
    assert ref_resp.headers.get("www-authenticate") == "Bearer"
    assert ref_resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_logout_is_idempotent(async_client: AsyncClient) -> None:
    """Verify repeating logout with the same token returns 200 OK without errors."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"idemp_{unique_id}@example.com"
    username = f"idemp_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    refresh_token = login_resp.json()["refresh_token"]

    # First logout
    resp1 = await async_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert resp1.status_code == 200

    # Second logout (idempotent)
    resp2 = await async_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert resp2.status_code == 200
    assert resp2.json()["message"] == "Successfully logged out."

    # Third logout (idempotent)
    resp3 = await async_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert resp3.status_code == 200


@pytest.mark.asyncio
async def test_user_a_cannot_revoke_user_b_refresh_session(async_client: AsyncClient) -> None:
    """Verify that a token presenting User A's credentials cannot revoke User B's session."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    settings = get_settings()
    uid1 = uuid.uuid4().hex[:8]
    uid2 = uuid.uuid4().hex[:8]

    # Create User A
    await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"usera_{uid1}@example.com",
            "username": f"usera_{uid1}",
            "password": "Password123!",
        },
    )
    login_a = await async_client.post(
        "/api/v1/auth/login",
        json={"email": f"usera_{uid1}@example.com", "password": "Password123!"},
    )
    tok_a = login_a.json()["refresh_token"]
    pay_a = jwt.decode(tok_a, options={"verify_signature": False})

    # Create User B
    await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": f"userb_{uid2}@example.com",
            "username": f"userb_{uid2}",
            "password": "Password123!",
        },
    )
    login_b = await async_client.post(
        "/api/v1/auth/login",
        json={"email": f"userb_{uid2}@example.com", "password": "Password123!"},
    )
    tok_b = login_b.json()["refresh_token"]
    pay_b = jwt.decode(tok_b, options={"verify_signature": False})

    # Forge a token signed with valid key, but subject=User A and jti=User B's JTI
    forged_token = jwt.encode(
        {
            "sub": pay_a["sub"],  # User A
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(days=7)).timestamp()),
            "jti": pay_b["jti"],  # User B's JTI
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "type": "refresh",
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    # Attempt logout with forged token
    logout_resp = await async_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": forged_token},
    )
    assert logout_resp.status_code == 401
    assert logout_resp.json()["detail"] == "Invalid refresh token."

    # Confirm User B's session in PostgreSQL is NOT revoked
    async with get_session_factory()() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == pay_b["jti"]))
        b_sess = result.scalar_one()
        assert b_sess.revoked_at is None
        assert b_sess.is_active is True


@pytest.mark.asyncio
async def test_access_token_cannot_be_used_at_logout(async_client: AsyncClient) -> None:
    """Verify passing an access token to /logout returns 401."""
    token = create_access_token(subject=str(uuid.uuid4()))
    resp = await async_client.post("/api/v1/auth/logout", json={"refresh_token": token})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_tampered_and_wrong_secret_refresh_token_cannot_logout(
    async_client: AsyncClient,
) -> None:
    """Verify tampered and wrong-secret refresh tokens return 401 on logout."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"tamper_{unique_id}@example.com"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": f"tamper_{unique_id}", "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    valid_token = login_resp.json()["refresh_token"]

    # 1. Tampered signature
    tampered = valid_token[:-4] + "xxxx"
    resp_tamper = await async_client.post("/api/v1/auth/logout", json={"refresh_token": tampered})
    assert resp_tamper.status_code == 401
    assert resp_tamper.json()["detail"] == "Invalid refresh token."

    # 2. Wrong secret
    settings = get_settings()
    wrong_secret_tok = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(days=7)).timestamp()),
            "jti": uuid.uuid4().hex,
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "type": "refresh",
        },
        "wrong_secret_key_123456789012345678",
        algorithm="HS256",
    )
    resp_secret = await async_client.post(
        "/api/v1/auth/logout", json={"refresh_token": wrong_secret_tok}
    )
    assert resp_secret.status_code == 401
    assert resp_secret.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_user_deletion_cascades_and_removes_refresh_tokens(
    async_client: AsyncClient,
) -> None:
    """Verify deleting a User record from PostgreSQL cascades and deletes refresh_tokens."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"cascade_{unique_id}@example.com"
    username = f"cascade_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    refresh_token = login_resp.json()["refresh_token"]
    payload = jwt.decode(refresh_token, options={"verify_signature": False})
    jti = payload["jti"]

    # Verify session exists in DB
    async with get_session_factory()() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        assert result.scalar_one_or_none() is not None

        # Delete user
        user_res = await session.execute(select(User).where(User.email == email.lower()))
        user = user_res.scalar_one()
        await session.delete(user)
        await session.commit()

    # Verify refresh_tokens row was deleted via ON DELETE CASCADE
    async with get_session_factory()() as session:
        result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        assert result.scalar_one_or_none() is None

    # Refresh attempt must fail with 401
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert ref_resp.status_code == 401
    assert ref_resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_openapi_contains_logout_endpoint(async_client: AsyncClient) -> None:
    """Verify OpenAPI documentation includes POST /api/v1/auth/logout with correct schema."""
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    openapi_doc = resp.json()

    paths = openapi_doc["paths"]
    assert "/api/v1/auth/logout" in paths
    logout_spec = paths["/api/v1/auth/logout"]
    assert "post" in logout_spec
    post_spec = logout_spec["post"]

    # Verify no Bearer security is required on /logout body endpoint
    assert "security" not in post_spec or post_spec["security"] == []

    # Verify response schema references
    assert "200" in post_spec["responses"]
    assert "401" in post_spec["responses"]
