"""Integration tests for refresh token flow, endpoint, security, and lifecycle."""

import uuid
from datetime import timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token
from app.db.models.user import User
from app.db.session import get_db_session
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_login_returns_both_tokens(async_client: AsyncClient) -> None:
    """Verify that login returns both access_token and refresh_token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"tokens_{unique_id}@example.com"
    username = f"tokens_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # Register
    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    try:
        # Login
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200
        data = login_resp.json()

        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert data["access_token"] != data["refresh_token"]
    finally:
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            u = await session.get(User, user_id)
            if u:
                await session.delete(u)
                await session.commit()
        except Exception:
            await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_successful_refresh_flow(async_client: AsyncClient) -> None:
    """Verify refresh token exchanges for a new access token that can access /me."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"ref_{unique_id}@example.com"
    username = f"ref_{unique_id}"
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
    login_data = login_resp.json()
    orig_access = login_data["access_token"]
    refresh_tok = login_data["refresh_token"]

    try:
        # 2. Call /refresh
        refresh_resp = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert refresh_resp.status_code == 200
        ref_data = refresh_resp.json()

        assert "access_token" in ref_data
        assert ref_data["token_type"] == "bearer"
        assert ref_data["expires_in"] == get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        new_access = ref_data["access_token"]
        assert new_access != orig_access

        # 3. Use new access token on /me
        me_resp = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_access}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == email.lower()
    finally:
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            user_res = await session.execute(select(User).where(User.email == email.lower()))
            u = user_res.scalar_one_or_none()
            if u:
                await session.delete(u)
                await session.commit()
        except Exception:
            await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_refresh_token_cannot_access_me_endpoint(async_client: AsyncClient) -> None:
    """Verify that a refresh token is rejected if passed to /me as a Bearer access token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    user_id = uuid.uuid4()
    refresh_token = create_refresh_token(subject=user_id)

    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_access_token_cannot_be_used_at_refresh_endpoint(
    async_client: AsyncClient,
) -> None:
    """Verify that an access token is rejected by /refresh."""
    user_id = uuid.uuid4()
    access_token = create_access_token(subject=user_id)

    resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": access_token},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_expired_refresh_token_returns_401(async_client: AsyncClient) -> None:
    """Verify expired refresh token returns 401."""
    user_id = uuid.uuid4()
    expired_token = create_refresh_token(
        subject=user_id,
        expires_delta=timedelta(seconds=-1),
    )

    resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": expired_token},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_tampered_refresh_token_returns_401(async_client: AsyncClient) -> None:
    """Verify tampered refresh token returns 401."""
    user_id = uuid.uuid4()
    token = create_refresh_token(subject=user_id)
    parts = token.split(".")
    tampered = f"{parts[0]}.{parts[1]}.{parts[2][:-4]}XXXX"

    resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tampered},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_wrong_secret_refresh_token_returns_401(async_client: AsyncClient) -> None:
    """Verify refresh token signed with wrong secret key returns 401."""
    settings = get_settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": 1000,
        "exp": 2000000000,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "refresh",
    }
    wrong_token = jwt.encode(payload, "different_secret_key_1234567890123456", algorithm="HS256")

    resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": wrong_token},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_missing_refresh_token_payload_returns_422(async_client: AsyncClient) -> None:
    """Verify missing refresh_token field in body returns 422."""
    resp = await async_client.post("/api/v1/auth/refresh", json={})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_malformed_refresh_token_returns_401(async_client: AsyncClient) -> None:
    """Verify malformed non-JWT string returns 401."""
    resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "malformed.string.not.jwt"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_inactive_user_cannot_refresh(async_client: AsyncClient) -> None:
    """Verify inactive user cannot exchange refresh token for access token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"inact_ref_{unique_id}@example.com"
    username = f"inact_ref_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # Register & Login
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    refresh_tok = login_resp.json()["refresh_token"]

    # Deactivate user in PostgreSQL
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user_res = await session.execute(select(User).where(User.email == email.lower()))
        u = user_res.scalar_one()
        u.is_active = False
        await session.commit()
        user_id = u.id
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass

    try:
        # Attempt refresh
        ref_resp = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert ref_resp.status_code == 401
        assert ref_resp.headers.get("www-authenticate") == "Bearer"
        assert "inactive" in ref_resp.json()["detail"].lower()
    finally:
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            del_u = await session.get(User, user_id)
            if del_u:
                await session.delete(del_u)
                await session.commit()
        except Exception:
            await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_deleted_user_cannot_refresh(async_client: AsyncClient) -> None:
    """Verify deleted user cannot refresh token."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"del_ref_{unique_id}@example.com"
    username = f"del_ref_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # Register & Login
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    refresh_tok = login_resp.json()["refresh_token"]

    # Delete user from PostgreSQL
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user_res = await session.execute(select(User).where(User.email == email.lower()))
        u = user_res.scalar_one()
        await session.delete(u)
        await session.commit()
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass

    # Attempt refresh
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_tok},
    )
    assert ref_resp.status_code == 401
    assert ref_resp.headers.get("www-authenticate") == "Bearer"
    assert ref_resp.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_single_use_rotation_and_reuse_rejection(async_client: AsyncClient) -> None:
    """Verify single-use token rotation and that reusing consumed token triggers 401."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"multi_ref_{unique_id}@example.com"
    username = f"multi_ref_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # Register & Login
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    refresh_tok = login_resp.json()["refresh_token"]

    try:
        # First refresh: consumes refresh_tok, issues rotated token
        ref1 = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert ref1.status_code == 200
        data1 = ref1.json()
        assert "access_token" in data1
        assert "refresh_token" in data1
        rotated_tok = data1["refresh_token"]
        assert rotated_tok != refresh_tok

        # Reusing consumed refresh_tok -> Reuse detected -> 401
        ref_reuse = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_tok},
        )
        assert ref_reuse.status_code == 401
        assert ref_reuse.json()["detail"] == "Invalid refresh token."

        # Child token in compromised family also revoked -> 401
        ref_child = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": rotated_tok},
        )
        assert ref_child.status_code == 401
        assert ref_child.json()["detail"] == "Invalid refresh token."
    finally:
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            user_res = await session.execute(select(User).where(User.email == email.lower()))
            u = user_res.scalar_one_or_none()
            if u:
                await session.delete(u)
                await session.commit()
        except Exception:
            await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_openapi_contains_refresh_endpoint(async_client: AsyncClient) -> None:
    """Verify OpenAPI document contains POST /api/v1/auth/refresh and schemas."""
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    paths = spec["paths"]
    assert "/api/v1/auth/refresh" in paths

    refresh_op = paths["/api/v1/auth/refresh"]["post"]
    assert "Refresh access token" in refresh_op["summary"]
    assert "200" in refresh_op["responses"]
    assert "401" in refresh_op["responses"]

    # Refresh should NOT have HTTPBearer security requirement
    assert "security" not in refresh_op or len(refresh_op["security"]) == 0

    components = spec.get("components", {}).get("schemas", {})
    assert "RefreshTokenRequest" in components
    assert "refresh_token" in components["RefreshTokenRequest"]["properties"]
