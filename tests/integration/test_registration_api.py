"""Integration tests for user registration endpoint against live PostgreSQL."""

import logging
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.security import verify_password
from app.db.models.role import Role
from app.db.models.user import User
from app.db.session import get_db_session
from app.schemas.auth import UserRegisterRequest
from app.services.auth import (
    DefaultRoleNotFoundError,
    register_user,
)
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_successful_registration_api_and_db_state(async_client: AsyncClient) -> None:
    """Verify end-to-end registration flow, safe response, and default role assignment."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"register_{unique_id}@example.com"
    username = f"user_{unique_id}"
    password = f"P@ssw0rd_{unique_id}!"

    payload = {
        "email": email,
        "username": username,
        "password": password,
    }

    # 1. HTTP Request to registration endpoint
    response = await async_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, (
        f"Expected 201 Created, got {response.status_code}: {response.text}"
    )

    data = response.json()

    # 2. Verify safe response schema (no credentials or tokens)
    assert "id" in data
    assert data["email"] == email.lower()
    assert data["username"] == username
    assert data["is_active"] is True
    assert data["is_verified"] is False
    assert "created_at" in data
    assert "password" not in data
    assert "password_hash" not in data
    assert "access_token" not in data
    assert "refresh_token" not in data
    assert "token_type" not in data

    user_id = uuid.UUID(data["id"])

    # 3. Direct verification in PostgreSQL
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user_result = await session.execute(
            select(User).where(User.id == user_id).options(selectinload(User.roles))
        )
        persisted_user = user_result.scalar_one_or_none()
        assert persisted_user is not None
        assert persisted_user.email == email.lower()
        assert persisted_user.username == username

        # Verify password hash
        assert persisted_user.password_hash.startswith("$argon2id$")
        assert password not in persisted_user.password_hash
        assert verify_password(password, persisted_user.password_hash) is True

        # Verify default role assignment
        role_names = [role.name for role in persisted_user.roles]
        assert role_names == ["user"], f"User should have exactly ['user'] role, got: {role_names}"
        assert "admin" not in role_names
    finally:
        # Cleanup
        try:
            if persisted_user is not None:
                await session.delete(persisted_user)
                await session.commit()
        except Exception:
            await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_duplicate_email_registration_returns_409(async_client: AsyncClient) -> None:
    """Registering with an existing email (exact or case-variant) returns HTTP 409 Conflict."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"dup_email_{unique_id}@example.com"
    username_a = f"dup_a_{unique_id}"
    username_b = f"dup_b_{unique_id}"
    password = "SecurePassword123!"

    # First registration
    resp1 = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username_a, "password": password},
    )
    assert resp1.status_code == 201

    user1_id = uuid.UUID(resp1.json()["id"])

    try:
        # Second registration with exact same email
        resp2 = await async_client.post(
            "/api/v1/auth/register",
            json={"email": email, "username": username_b, "password": password},
        )
        assert resp2.status_code == 409
        assert "email" in resp2.json()["detail"].lower()

        # Third registration with uppercase variation of the same email
        resp3 = await async_client.post(
            "/api/v1/auth/register",
            json={"email": email.upper(), "username": f"dup_c_{unique_id}", "password": password},
        )
        assert resp3.status_code == 409
        assert "email" in resp3.json()["detail"].lower()

        # Verify only one user exists in PostgreSQL for this email
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            count_result = await session.execute(
                select(func.count(User.id)).where(func.lower(User.email) == email.lower())
            )
            count = count_result.scalar_one()
            assert count == 1
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
            u = await session.get(User, user1_id)
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
async def test_duplicate_username_registration_returns_409(async_client: AsyncClient) -> None:
    """Registering with an existing username (exact or case-variant) returns HTTP 409 Conflict."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email_a = f"user_a_{unique_id}@example.com"
    email_b = f"user_b_{unique_id}@example.com"
    email_c = f"user_c_{unique_id}@example.com"
    username = f"uniq_name_{unique_id}"
    password = "SecurePassword123!"

    # First registration
    resp1 = await async_client.post(
        "/api/v1/auth/register",
        json={"email": email_a, "username": username, "password": password},
    )
    assert resp1.status_code == 201
    user1_id = uuid.UUID(resp1.json()["id"])

    try:
        # Second registration with exact same username
        resp2 = await async_client.post(
            "/api/v1/auth/register",
            json={"email": email_b, "username": username, "password": password},
        )
        assert resp2.status_code == 409
        assert "username" in resp2.json()["detail"].lower()

        # Third registration with uppercase variation of the same username
        resp3 = await async_client.post(
            "/api/v1/auth/register",
            json={"email": email_c, "username": username.upper(), "password": password},
        )
        assert resp3.status_code == 409
        assert "username" in resp3.json()["detail"].lower()

        # Verify only one user exists in PostgreSQL for this username
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            count_result = await session.execute(
                select(func.count(User.id)).where(func.lower(User.username) == username.lower())
            )
            count = count_result.scalar_one()
            assert count == 1
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
            u = await session.get(User, user1_id)
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
async def test_transaction_rollback_prevents_partial_user() -> None:
    """If default role assignment or commit fails, transaction rollback leaves no user in DB."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"rollback_{unique_id}@example.com"
    username = f"rollback_{unique_id}"
    req = UserRegisterRequest(
        email=email,
        username=username,
        password="SecurePassword123!",
    )

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        # Temporarily mock Role lookup to simulate missing default role
        original_execute = session.execute

        async def failing_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
            stmt = args[0]
            # If checking for Role, return empty
            if hasattr(stmt, "is_select") and stmt.is_select:
                from sqlalchemy.sql.selectable import Select

                if isinstance(stmt, Select):
                    for entity in stmt.column_descriptions:
                        if entity.get("type") == Role:
                            # Mock empty result
                            from unittest.mock import MagicMock

                            empty_res = MagicMock()
                            empty_res.scalar_one_or_none.return_value = None
                            return empty_res
            return await original_execute(*args, **kwargs)

        session.execute = failing_execute  # type: ignore[method-assign]

        with pytest.raises(DefaultRoleNotFoundError):
            await register_user(session, req)

        # Restore
        session.execute = original_execute  # type: ignore[method-assign]

        # Verify no partial user exists in PostgreSQL
        check = await session.execute(select(User).where(User.email == email))
        assert check.scalar_one_or_none() is None
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_privilege_escalation_payload_rejected_by_api(async_client: AsyncClient) -> None:
    """Extra fields such as is_admin, role, id are rejected with 422 Unprocessable Entity."""
    payload = {
        "email": "hacker@example.com",
        "username": "hacker_user",
        "password": "SecurePassword123!",
        "role": "admin",
        "is_admin": True,
        "is_superuser": True,
    }
    response = await async_client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422
    err_body = response.json()
    assert "detail" in err_body


@pytest.mark.asyncio
async def test_openapi_schema_contains_auth_register(async_client: AsyncClient) -> None:
    """FastAPI OpenAPI documentation includes /api/v1/auth/register with correct status codes."""
    response = await async_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()

    assert "/api/v1/auth/register" in spec["paths"]
    register_op = spec["paths"]["/api/v1/auth/register"]["post"]
    assert register_op["summary"] == "Register a new user account"

    # Status codes documented
    responses = register_op["responses"]
    assert "201" in responses
    assert "400" in responses
    assert "409" in responses
    assert "422" in responses

    # Check request body schema does not mention password_hash
    components = spec.get("components", {}).get("schemas", {})
    req_schema = components.get("UserRegisterRequest", {})
    assert "password_hash" not in req_schema.get("properties", {})
    assert "password" in req_schema.get("properties", {})

    # Check response body schema does not mention password or password_hash
    resp_schema = components.get("UserRegisterResponse", {})
    assert "password" not in resp_schema.get("properties", {})
    assert "password_hash" not in resp_schema.get("properties", {})


@pytest.mark.asyncio
async def test_password_never_logged_during_registration(
    async_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Ensure plaintext passwords and password hashes do not leak into application logs."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    secret_password = f"SuperSecret123_{unique_id}!"
    payload = {
        "email": f"logcheck_{unique_id}@example.com",
        "username": f"logcheck_{unique_id}",
        "password": secret_password,
    }

    with caplog.at_level(logging.DEBUG):
        response = await async_client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 201
    user_id = response.json()["id"]

    for record in caplog.records:
        # Plaintext password must never appear in any log record (including engine logs)
        assert secret_password not in record.message
        # Application loggers must never log password hashes or raw secrets
        if record.name.startswith("app"):
            assert "$argon2id$" not in record.message

    # Cleanup
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
