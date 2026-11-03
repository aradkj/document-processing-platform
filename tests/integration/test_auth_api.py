"""Integration tests for Login, JWT authentication, and /me endpoint against live PostgreSQL."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models.user import User
from app.db.session import get_db_session
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_successful_login_and_token_issuance(async_client: AsyncClient) -> None:
    """Verify login with valid credentials returns 200, JWT token, and updates last_login_at."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"login_user_{unique_id}@example.com"
    username = f"login_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # 1. Register a user
    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    try:
        # 2. Login with valid credentials
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200
        token_data = login_resp.json()

        assert "access_token" in token_data
        assert "refresh_token" in token_data
        assert token_data["token_type"] == "bearer"
        assert token_data["expires_in"] == get_settings().JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert "password" not in token_data
        assert "password_hash" not in token_data

        # 3. Direct DB check for updated last_login_at
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            persisted_user = await session.get(User, user_id)
            assert persisted_user is not None
            assert persisted_user.last_login_at is not None
            # Verified within last 60 seconds
            diff = abs((datetime.now(UTC) - persisted_user.last_login_at).total_seconds())
            assert diff < 60
        finally:
            try:
                await anext(session_gen)
            except StopAsyncIteration:
                pass
    finally:
        # Cleanup
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
async def test_login_invalid_password_returns_401(async_client: AsyncClient) -> None:
    """Verify wrong password returns generic 401 with WWW-Authenticate and no last_login update."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"wrongpw_{unique_id}@example.com"
    username = f"wrongpw_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    try:
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "WrongPasswordAttempt123!"},
        )
        assert login_resp.status_code == 401
        assert login_resp.headers.get("www-authenticate") == "Bearer"
        assert login_resp.json()["detail"] == "Invalid email or password."

        # Verify last_login_at was NOT updated
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            persisted_user = await session.get(User, user_id)
            assert persisted_user is not None
            assert persisted_user.last_login_at is None
        finally:
            try:
                await anext(session_gen)
            except StopAsyncIteration:
                pass
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
async def test_login_unknown_email_returns_identical_401(async_client: AsyncClient) -> None:
    """Verify unknown email returns the exact same 401 response to mitigate enumeration."""
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": "nonexistent_user_999@example.com", "password": "RandomPassword123!"},
    )
    assert login_resp.status_code == 401
    assert login_resp.headers.get("www-authenticate") == "Bearer"
    assert login_resp.json()["detail"] == "Invalid email or password."


@pytest.mark.asyncio
async def test_login_case_insensitive_email(async_client: AsyncClient) -> None:
    """Verify login works when email casing differs from registration."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"case_user_{unique_id}@example.com"
    username = f"case_user_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    try:
        # Attempt login using uppercase email
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email.upper(), "password": password},
        )
        assert login_resp.status_code == 200
        assert "access_token" in login_resp.json()
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
async def test_login_inactive_user_returns_401(async_client: AsyncClient) -> None:
    """Verify inactive user cannot log in and receives 401."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"inactive_{unique_id}@example.com"
    username = f"inactive_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    # Deactivate user in PostgreSQL
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        u = await session.get(User, user_id)
        assert u is not None
        u.is_active = False
        await session.commit()
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass

    try:
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 401
        assert login_resp.headers.get("www-authenticate") == "Bearer"
        assert "inactive" in login_resp.json()["detail"].lower()
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
async def test_current_user_me_endpoint_success(async_client: AsyncClient) -> None:
    """Verify GET /api/v1/auth/me resolves current authenticated user profile."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"me_{unique_id}@example.com"
    username = f"me_user_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # 1. Register
    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = reg_resp.json()["id"]

    try:
        # 2. Login
        login_resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]

        # 3. Access /me
        me_resp = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        me_data = me_resp.json()

        assert me_data["id"] == user_id
        assert me_data["email"] == email.lower()
        assert me_data["username"] == username
        assert me_data["is_active"] is True
        assert me_data["is_verified"] is False
        assert me_data["last_login_at"] is not None
        assert "created_at" in me_data
        assert "password" not in me_data
        assert "password_hash" not in me_data
    finally:
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            u = await session.get(User, uuid.UUID(user_id))
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
async def test_current_user_me_missing_or_invalid_bearer_token(
    async_client: AsyncClient,
) -> None:
    """Verify /me rejects missing token or invalid authorization schemes with 401."""
    # 1. Missing Authorization header
    resp1 = await async_client.get("/api/v1/auth/me")
    assert resp1.status_code == 401
    assert resp1.headers.get("www-authenticate") == "Bearer"

    # 2. Non-Bearer scheme
    resp2 = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp2.status_code == 401
    assert resp2.headers.get("www-authenticate") == "Bearer"

    # 3. Malformed token
    resp3 = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer malformed.token.value"},
    )
    assert resp3.status_code == 401
    assert resp3.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_current_user_me_expired_token_returns_401(
    async_client: AsyncClient,
) -> None:
    """Verify expired token is rejected when accessing /me."""
    expired_token = create_access_token(
        subject=uuid.uuid4(),
        expires_delta=timedelta(seconds=-10),
    )
    resp = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"
    assert "expired" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_current_user_me_deactivated_after_token_issuance(
    async_client: AsyncClient,
) -> None:
    """Verify user deactivated after token issuance cannot access /me."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"deact_{unique_id}@example.com"
    username = f"deact_user_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    reg_resp = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    assert reg_resp.status_code == 201
    user_id = uuid.UUID(reg_resp.json()["id"])

    # Login to obtain valid token
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]

    # Deactivate user in PostgreSQL
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        u = await session.get(User, user_id)
        assert u is not None
        u.is_active = False
        await session.commit()
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass

    try:
        # Access /me with the previously valid token
        me_resp = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 401
        assert me_resp.headers.get("www-authenticate") == "Bearer"
        assert "inactive" in me_resp.json()["detail"].lower()
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
async def test_openapi_schema_contains_auth_endpoints(async_client: AsyncClient) -> None:
    """Verify OpenAPI document contains login and me routes with security requirements."""
    resp = await async_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    # Verify routes exist
    paths = spec["paths"]
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/me" in paths

    # Login route checks
    login_op = paths["/api/v1/auth/login"]["post"]
    assert "200" in login_op["responses"]
    assert "401" in login_op["responses"]

    # Me route checks
    me_op = paths["/api/v1/auth/me"]["get"]
    assert "200" in me_op["responses"]
    assert "401" in me_op["responses"]
    # Check security requirement on /me
    assert "security" in me_op
    security_reqs = me_op["security"]
    assert any("HTTPBearer" in req for req in security_reqs)

    # Check components
    components = spec.get("components", {}).get("schemas", {})
    assert "UserLoginRequest" in components
    assert "TokenResponse" in components
    assert "UserMeResponse" in components
    assert "password_hash" not in components["TokenResponse"].get("properties", {})
    assert "password_hash" not in components["UserMeResponse"].get("properties", {})


@pytest.mark.asyncio
async def test_no_sensitive_secrets_in_logs_during_login(
    async_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Verify that credentials and JWT secrets do not leak into application logs."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    secret_pw = f"TopSecretPw123_{unique_id}!"
    email = f"loglogin_{unique_id}@example.com"
    username = f"loglogin_{unique_id}"

    # Register
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": secret_pw},
    )

    with caplog.at_level(logging.DEBUG):
        resp = await async_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": secret_pw},
        )
    assert resp.status_code == 200

    jwt_secret = get_settings().JWT_SECRET_KEY
    for record in caplog.records:
        assert secret_pw not in record.message
        assert jwt_secret not in record.message
        if record.name.startswith("app"):
            assert "$argon2id$" not in record.message

    # Cleanup
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        del_user = await session.execute(select(User).where(User.email == email))
        u = del_user.scalar_one_or_none()
        if u:
            await session.delete(u)
            await session.commit()
    except Exception:
        await session.rollback()
    try:
        await anext(session_gen)
    except StopAsyncIteration:
        pass
