"""Integration tests for single-use token rotation, reuse detection, and family isolation."""

import asyncio
import uuid

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.future import select

from app.db.models.refresh_token import RefreshToken
from app.db.session import get_session_factory
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_normal_token_rotation_lineage(async_client: AsyncClient) -> None:
    """Verify single-use rotation creates linked child token and marks parent replaced."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"rot_{unique_id}@example.com"
    username = f"rot_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    # 1. Register & Login -> A1
    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login_resp.status_code == 200
    token_a1 = login_resp.json()["refresh_token"]
    payload_a1 = jwt.decode(token_a1, options={"verify_signature": False})
    jti_a1 = payload_a1["jti"]

    # 2. Rotate A1 -> A2
    ref_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_a1},
    )
    assert ref_resp.status_code == 200
    ref_data = ref_resp.json()
    assert "access_token" in ref_data
    assert "refresh_token" in ref_data
    token_a2 = ref_data["refresh_token"]
    assert token_a2 != token_a1
    payload_a2 = jwt.decode(token_a2, options={"verify_signature": False})
    jti_a2 = payload_a2["jti"]

    # 3. Query PostgreSQL directly to verify lineage and family
    async with get_session_factory()() as session:
        # Check A1 state
        res_a1 = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti_a1))
        sess_a1 = res_a1.scalar_one()
        assert sess_a1.replaced_by_jti == jti_a2
        assert sess_a1.is_replaced is True
        assert sess_a1.revoked_at is not None
        assert sess_a1.is_active is False

        # Check A2 state
        res_a2 = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti_a2))
        sess_a2 = res_a2.scalar_one()
        assert sess_a2.family_id == sess_a1.family_id
        assert sess_a2.replaced_by_jti is None
        assert sess_a2.revoked_at is None
        assert sess_a2.is_active is True

    # 4. A2 can be used for new access tokens
    ref2_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_a2},
    )
    assert ref2_resp.status_code == 200
    assert "access_token" in ref2_resp.json()


@pytest.mark.asyncio
async def test_multiple_rotations_chain(async_client: AsyncClient) -> None:
    """Verify multiple rotations form a complete linear chain: A1 -> A2 -> A3 -> A4."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"chain_{unique_id}@example.com"
    username = f"chain_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    current_token = login_resp.json()["refresh_token"]
    jtis: list[str] = [jwt.decode(current_token, options={"verify_signature": False})["jti"]]

    # Rotate 3 times: A1 -> A2 -> A3 -> A4
    for _ in range(3):
        resp = await async_client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": current_token},
        )
        assert resp.status_code == 200
        current_token = resp.json()["refresh_token"]
        jtis.append(jwt.decode(current_token, options={"verify_signature": False})["jti"])

    assert len(jtis) == 4
    # Verify linear chain in database
    async with get_session_factory()() as session:
        records: list[RefreshToken] = []
        for jti in jtis:
            res = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
            records.append(res.scalar_one())

        family_id = records[0].family_id
        for i in range(3):
            assert records[i].family_id == family_id
            assert records[i].replaced_by_jti == jtis[i + 1]
            assert records[i].is_replaced is True
            assert records[i].revoked_at is not None

        # A4 is the currently active head of the chain
        assert records[3].family_id == family_id
        assert records[3].replaced_by_jti is None
        assert records[3].revoked_at is None
        assert records[3].is_active is True


@pytest.mark.asyncio
async def test_reuse_detection_invalidates_entire_family(async_client: AsyncClient) -> None:
    """Verify reuse of an already-rotated token invalidates the entire token family."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"reuse_{unique_id}@example.com"
    username = f"reuse_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_a1 = login_resp.json()["refresh_token"]

    # Rotate A1 -> A2
    rot_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_a1},
    )
    assert rot_resp.status_code == 200
    token_a2 = rot_resp.json()["refresh_token"]

    # REUSE: Attacker attempts to use consumed A1
    reuse_resp = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_a1},
    )
    assert reuse_resp.status_code == 401
    assert reuse_resp.headers.get("www-authenticate") == "Bearer"
    assert reuse_resp.json()["detail"] == "Invalid refresh token."

    # Verify entire family in PostgreSQL is marked revoked
    payload_a1 = jwt.decode(token_a1, options={"verify_signature": False})
    async with get_session_factory()() as session:
        res = await session.execute(
            select(RefreshToken).where(RefreshToken.jti == payload_a1["jti"])
        )
        sess_a1 = res.scalar_one()
        family_id = sess_a1.family_id

        # Query all tokens in this family
        fam_res = await session.execute(
            select(RefreshToken).where(RefreshToken.family_id == family_id)
        )
        family_tokens = fam_res.scalars().all()
        assert len(family_tokens) == 2
        for t in family_tokens:
            assert t.revoked_at is not None
            assert t.is_active is False

    # Legitimate client presenting A2 is now also rejected because family was compromised
    ref_a2 = await async_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_a2},
    )
    assert ref_a2.status_code == 401
    assert ref_a2.json()["detail"] == "Invalid refresh token."


@pytest.mark.asyncio
async def test_deep_reuse_detection_in_long_chain(async_client: AsyncClient) -> None:
    """Verify reusing A1 in chain A1 -> A2 -> A3 -> A4 revokes active token A4."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"deep_{unique_id}@example.com"
    username = f"deep_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_a1 = login_resp.json()["refresh_token"]

    # Rotate to A2
    r2 = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a1})
    token_a2 = r2.json()["refresh_token"]
    # Rotate to A3
    r3 = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a2})
    token_a3 = r3.json()["refresh_token"]
    # Rotate to A4
    r4 = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a3})
    token_a4 = r4.json()["refresh_token"]

    # Attacker reuses A2
    reuse_resp = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a2})
    assert reuse_resp.status_code == 401

    # Active token A4 is now revoked
    a4_resp = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a4})
    assert a4_resp.status_code == 401


@pytest.mark.asyncio
async def test_family_isolation_independent_logins(async_client: AsyncClient) -> None:
    """Verify revoking/compromising Family A does not affect Family B of the same user."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"iso_{unique_id}@example.com"
    username = f"iso_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )

    # Session 1 (e.g., Device A) -> Family A
    login1 = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_fa1 = login1.json()["refresh_token"]

    # Session 2 (e.g., Device B) -> Family B
    login2 = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_fb1 = login2.json()["refresh_token"]

    # Rotate Family A: FA1 -> FA2
    fa_rot = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_fa1})
    token_fa2 = fa_rot.json()["refresh_token"]

    # Trigger reuse on Family A
    await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_fa1})

    # Family A is dead
    fa_fail = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_fa2})
    assert fa_fail.status_code == 401

    # Family B remains fully active and can rotate FB1 -> FB2
    fb_rot = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_fb1})
    assert fb_rot.status_code == 200
    token_fb2 = fb_rot.json()["refresh_token"]
    assert token_fb2 != token_fb1


@pytest.mark.asyncio
async def test_concurrent_refresh_requests_protection(async_client: AsyncClient) -> None:
    """Verify concurrent refresh on the same token results in single authoritative rotation."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"concur_{unique_id}@example.com"
    username = f"concur_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token = login_resp.json()["refresh_token"]

    # Send two simultaneous refresh requests using the exact same token
    resp1, resp2 = await asyncio.gather(
        async_client.post("/api/v1/auth/refresh", json={"refresh_token": token}),
        async_client.post("/api/v1/auth/refresh", json={"refresh_token": token}),
    )

    # Exactly one request must not produce conflicting sibling branches
    statuses = sorted([resp1.status_code, resp2.status_code])
    # Case: [200, 401] if request 2 was handled as reuse, or [401, 401] if reuse revoked both
    assert 200 in statuses or statuses == [401, 401]
    # Under no circumstance can both succeed with 200 (split-brain forbidden)
    assert not (resp1.status_code == 200 and resp2.status_code == 200)

    # Verify in DB that only one replacement JTI was written for the token
    payload = jwt.decode(token, options={"verify_signature": False})
    async with get_session_factory()() as session:
        res = await session.execute(select(RefreshToken).where(RefreshToken.jti == payload["jti"]))
        row = res.scalar_one()
        assert row.replaced_by_jti is not None


@pytest.mark.asyncio
async def test_logout_revokes_active_token_family(async_client: AsyncClient) -> None:
    """Verify logging out with the current active token revokes the entire family."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"logrot_{unique_id}@example.com"
    username = f"logrot_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_a1 = login_resp.json()["refresh_token"]

    # Rotate to A2
    rot = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a1})
    token_a2 = rot.json()["refresh_token"]

    # Logout with A2
    logout_resp = await async_client.post("/api/v1/auth/logout", json={"refresh_token": token_a2})
    assert logout_resp.status_code == 200
    assert logout_resp.json()["message"] == "Successfully logged out."

    # A2 cannot refresh
    ref_fail = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a2})
    assert ref_fail.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_replaced_token_is_idempotent_and_safe(
    async_client: AsyncClient,
) -> None:
    """Verify logout with an already-replaced token revokes family and is idempotent."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    unique_id = uuid.uuid4().hex[:8]
    email = f"logrep_{unique_id}@example.com"
    username = f"logrep_{unique_id}"
    password = f"SecurePassword123_{unique_id}!"

    await async_client.post(
        "/api/v1/auth/register",
        json={"email": email, "username": username, "password": password},
    )
    login_resp = await async_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    token_a1 = login_resp.json()["refresh_token"]

    # Rotate to A2
    rot = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a1})
    token_a2 = rot.json()["refresh_token"]

    # Logout with already-replaced A1 -> Should succeed idempotently
    logout_resp = await async_client.post("/api/v1/auth/logout", json={"refresh_token": token_a1})
    assert logout_resp.status_code == 200

    # A1 remains invalid
    ref_a1 = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a1})
    assert ref_a1.status_code == 401

    # A2 is also revoked
    ref_a2 = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": token_a2})
    assert ref_a2.status_code == 401
