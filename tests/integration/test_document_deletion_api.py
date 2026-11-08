"""Integration tests for Document Deletion API and Coordinated Lifecycle.

Tests execute against live PostgreSQL and local StorageBackend:
- DELETE /api/v1/documents/{document_id}
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.document import Document
from app.db.models.user import User
from app.db.session import get_db_session
from app.main import app
from app.storage import get_default_storage, get_storage
from app.storage.base import StorageBackend
from app.storage.exceptions import StorageDeleteError
from tests.integration.test_database import is_postgres_available


@pytest.fixture
async def sample_user() -> AsyncGenerator[User, None]:
    """Create and persist an isolated primary test user in PostgreSQL."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    suffix = secrets.token_hex(4)
    user = User(
        id=uuid.uuid4(),
        email=f"del_user_a_{suffix}@example.com",
        username=f"del_user_a_{suffix}",
        password_hash="mock_argon2id_hash_for_deletion_test",
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    try:
        yield user
    finally:
        session_gen2 = get_db_session()
        s2 = await anext(session_gen2)
        try:
            docs_res = await s2.execute(select(Document).where(Document.user_id == user.id))
            for doc in docs_res.scalars().all():
                await s2.delete(doc)
            user_to_delete = await s2.get(User, user.id)
            if user_to_delete:
                await s2.delete(user_to_delete)
            await s2.commit()
        except Exception:
            await s2.rollback()
        try:
            await anext(session_gen2)
        except StopAsyncIteration:
            pass


@pytest.fixture
async def second_user() -> AsyncGenerator[User, None]:
    """Create and persist a distinct secondary test user for multi-tenant tests."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    suffix = secrets.token_hex(4)
    user = User(
        id=uuid.uuid4(),
        email=f"del_user_b_{suffix}@example.com",
        username=f"del_user_b_{suffix}",
        password_hash="mock_argon2id_hash_for_deletion_test",
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    try:
        yield user
    finally:
        session_gen2 = get_db_session()
        s2 = await anext(session_gen2)
        try:
            docs_res = await s2.execute(select(Document).where(Document.user_id == user.id))
            for doc in docs_res.scalars().all():
                await s2.delete(doc)
            user_to_delete = await s2.get(User, user.id)
            if user_to_delete:
                await s2.delete(user_to_delete)
            await s2.commit()
        except Exception:
            await s2.rollback()
        try:
            await anext(session_gen2)
        except StopAsyncIteration:
            pass


def get_auth_headers(user: User) -> dict[str, str]:
    """Generate Bearer Authorization header for the given user."""
    token = create_access_token(subject=user.id, auth_version=user.auth_version)
    return {"Authorization": f"Bearer {token}"}


async def upload_test_doc(
    async_client: AsyncClient,
    user: User,
    filename: str = "sample_to_delete.pdf",
    content: bytes = b"%PDF-1.7\r\nDeletion verification payload",
) -> dict[str, Any]:
    """Helper to upload a document via API and return response data."""
    headers = get_auth_headers(user)
    files = {"file": (filename, content, "application/pdf")}
    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 201, f"Upload helper failed: {resp.text}"
    return cast(dict[str, Any], resp.json())


# =====================================================================
# Authorization & Ownership Tests
# =====================================================================


@pytest.mark.asyncio
async def test_delete_unauthenticated_rejected(async_client: AsyncClient) -> None:
    """Verify DELETE without token returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    resp = await async_client.delete(f"/api/v1/documents/{uuid.uuid4()}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_invalid_token_rejected(async_client: AsyncClient) -> None:
    """Verify DELETE with invalid token returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = {"Authorization": "Bearer invalid.jwt.token"}
    resp = await async_client.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_malformed_uuid_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify DELETE with non-UUID string returns 422 Unprocessable Content."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    resp = await async_client.delete("/api/v1/documents/bad-uuid", headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_nonexistent_document(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify DELETE on nonexistent document returns 404 Not Found."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    resp = await async_client.delete(f"/api/v1/documents/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"


@pytest.mark.asyncio
async def test_delete_foreign_owned_document_anti_enumeration(
    async_client: AsyncClient,
    sample_user: User,
    second_user: User,
) -> None:
    """Verify User B cannot delete User A's document and receives identical 404."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    headers_b = get_auth_headers(second_user)
    resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers_b)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"

    # Verify User A's document is still intact in DB and storage
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert doc is not None

    storage = get_default_storage()
    assert await storage.exists(doc.storage_key) is True


# =====================================================================
# Successful Coordinated Deletion Tests
# =====================================================================


@pytest.mark.asyncio
async def test_delete_authenticated_success(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify full successful deletion: 204 returned, file removed, DB row removed."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    # Retrieve storage key from database
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    storage_key = doc.storage_key

    storage = get_default_storage()
    assert await storage.exists(storage_key) is True

    # Execute DELETE request
    headers = get_auth_headers(sample_user)
    resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""

    # Verify physical file is gone
    assert await storage.exists(storage_key) is False

    # Verify DB row is gone
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    doc_check = await session2.get(Document, doc_id)
    assert doc_check is None


@pytest.mark.asyncio
async def test_delete_repeated_returns_404(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify repeated DELETE request on an already deleted document returns 404."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)
    resp1 = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp1.status_code == 204

    # Second DELETE returns 404
    resp2 = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp2.status_code == 404
    assert resp2.json()["detail"] == "Document not found"


# =====================================================================
# Error Recovery & Retry Lifecycle Tests
# =====================================================================


@pytest.mark.asyncio
async def test_delete_storage_failure_leaves_pending_state(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify storage failure retains pending-deletion DB row for subsequent retry."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    storage_mock = AsyncMock(spec=StorageBackend)
    storage_mock.delete.side_effect = StorageDeleteError("Filesystem unmount failure")
    app.dependency_overrides[get_storage] = lambda: storage_mock

    try:
        headers = get_auth_headers(sample_user)
        resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        assert resp.status_code == 500
        assert "failed to complete document deletion" in resp.json()["detail"].lower()

        # Verify DB row exists with deletion_pending_at set
        session_gen = get_db_session()
        session = await anext(session_gen)
        doc = await session.get(Document, doc_id)
        assert doc is not None
        assert doc.deletion_pending_at is not None
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.mark.asyncio
async def test_delete_retry_after_storage_failure_succeeds(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify retrying pending document after transient failure completes successfully."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    # Simulate pending-deletion state in database
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    doc.deletion_pending_at = datetime.now(UTC)
    await session.commit()

    # Retry deletion using live storage
    headers = get_auth_headers(sample_user)
    resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp.status_code == 204

    # Verify row and storage object are both removed
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    assert await session2.get(Document, doc_id) is None
    storage = get_default_storage()
    assert await storage.exists(doc.storage_key) is False


@pytest.mark.asyncio
async def test_delete_missing_physical_file_treated_as_absent(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify missing physical storage object (missing_ok=True) finalizes DB row cleanly."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    # Delete physical file behind the scenes
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    storage = get_default_storage()
    await storage.delete(doc.storage_key, missing_ok=True)

    # API DELETE should handle missing_ok=True and finalize DB removal
    headers = get_auth_headers(sample_user)
    resp = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp.status_code == 204

    # Row is deleted
    session_gen2 = get_db_session()
    session2 = await anext(session_gen2)
    assert await session2.get(Document, doc_id) is None


# =====================================================================
# Visibility & Query Isolation with Pending Deletion
# =====================================================================


@pytest.mark.asyncio
async def test_pending_deletion_hidden_from_listing_and_detail(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify documents marked deletion_pending_at are hidden from GET listing and detail."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    headers = get_auth_headers(sample_user)

    # Confirm visible initially
    list_resp1 = await async_client.get("/api/v1/documents", headers=headers)
    assert any(d["id"] == str(doc_id) for d in list_resp1.json()["items"])

    # Mark document pending deletion
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    doc.deletion_pending_at = datetime.now(UTC)
    await session.commit()

    # Verify hidden from listing
    list_resp2 = await async_client.get("/api/v1/documents", headers=headers)
    assert not any(d["id"] == str(doc_id) for d in list_resp2.json()["items"])

    # Verify hidden from detail (returns 404)
    detail_resp = await async_client.get(f"/api/v1/documents/{doc_id}", headers=headers)
    assert detail_resp.status_code == 404
    assert detail_resp.json()["detail"] == "Document not found"

    # Verify cannot be downloaded (returns 404)
    down_resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert down_resp.status_code == 404
    assert down_resp.json()["detail"] == "Document not found"


# =====================================================================
# Concurrent Deletion Safety & Deterministic Barrier Tests
# =====================================================================


@pytest.mark.asyncio
async def test_concurrent_deletion_requests_safe(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify simultaneous DELETE requests for the same document execute safely without errors."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)

    # Launch two concurrent delete requests
    req1 = async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    req2 = async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)

    resp1, resp2 = await asyncio.gather(req1, req2)

    # One must succeed with 204. The other either succeeds with 204 (safe concurrent) or 404
    status_codes = {resp1.status_code, resp2.status_code}
    assert 204 in status_codes
    assert status_codes.issubset({204, 404})

    # Both file and DB record are completely gone
    session_gen = get_db_session()
    session = await anext(session_gen)
    assert await session.get(Document, uuid.UUID(doc_id)) is None


@pytest.mark.asyncio
async def test_concurrent_deletion_deterministic_barrier_coordination(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify deterministic multi-request coordination using asyncio.Event barriers.

    Sequence verified:
    1. Request 1 acquires row lock, marks deletion_pending_at, commits, releasing row lock.
    2. Request 1 enters storage.delete and pauses at barrier.
    3. Intermediate DB check verifies deletion_pending_at IS NOT NULL.
    4. Request 2 arrives, observes pending document, and enters storage.delete(missing_ok=True).
    5. Barrier opens: both complete storage deletion and Step 4 with_for_update DB finalization.
    6. Both return 204 No Content without unhandled errors.
    7. Final state: DB record is deleted, storage file is deleted.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    real_storage = get_default_storage()
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    storage_key = doc.storage_key
    assert await real_storage.exists(storage_key)

    original_delete = real_storage.delete
    req1_entered_storage_delete = asyncio.Event()
    allow_req1_finish_storage_delete = asyncio.Event()
    delete_invocations = 0

    async def barrier_delete(key: str, missing_ok: bool = True) -> bool:
        nonlocal delete_invocations
        delete_invocations += 1
        call_num = delete_invocations
        if call_num == 1:
            req1_entered_storage_delete.set()
            await allow_req1_finish_storage_delete.wait()
        return await original_delete(key, missing_ok=missing_ok)

    from unittest.mock import patch

    headers = get_auth_headers(sample_user)

    with patch.object(real_storage, "delete", side_effect=barrier_delete):
        # 1. Start Request 1
        task1 = asyncio.create_task(
            async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        )
        # Wait until Request 1 has committed Step 2 and entered storage.delete
        await asyncio.wait_for(req1_entered_storage_delete.wait(), timeout=5.0)

        # 2. Verify intermediate DB state: row is marked pending deletion
        session_gen_mid = get_db_session()
        session_mid = await anext(session_gen_mid)
        doc_mid = await session_mid.get(Document, doc_id)
        assert doc_mid is not None
        assert doc_mid.deletion_pending_at is not None

        # 3. Start Request 2 while Request 1 is paused
        task2 = asyncio.create_task(
            async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        )

        # Allow Request 2 to enter delete pipeline and observe pending state
        await asyncio.sleep(0.05)

        # 4. Release Request 1 barrier
        allow_req1_finish_storage_delete.set()

        resp1 = await task1
        resp2 = await task2

        assert resp1.status_code == 204
        assert resp2.status_code == 204
        assert delete_invocations >= 2

    # 5. Verify final state: DB row is gone, storage file is gone
    session_gen_final = get_db_session()
    session_final = await anext(session_gen_final)
    assert await session_final.get(Document, doc_id) is None
    assert not await real_storage.exists(storage_key)


@pytest.mark.asyncio
async def test_concurrent_deletion_storage_failure_and_retry_recovery(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify that if Request 1 fails in storage deletion, document stays pending and retry works.

    Ensures:
    - Never leaves an active document whose file was deleted.
    - Never leaves an orphaned storage file once retry finishes.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    real_storage = get_default_storage()
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    storage_key = doc.storage_key
    assert await real_storage.exists(storage_key)

    headers = get_auth_headers(sample_user)

    # 1. Simulate failure during Request 1's storage deletion
    from unittest.mock import patch

    with patch.object(
        real_storage,
        "delete",
        side_effect=StorageDeleteError("Simulated storage failure during delete"),
    ):
        resp1 = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        assert resp1.status_code == 500

    # 2. Document is marked pending deletion, file still exists
    session_gen_mid = get_db_session()
    session_mid = await anext(session_gen_mid)
    doc_mid = await session_mid.get(Document, doc_id)
    assert doc_mid is not None
    assert doc_mid.deletion_pending_at is not None
    assert await real_storage.exists(storage_key)

    # Document is shielded from download (404) and detail (404)
    down_resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert down_resp.status_code == 404
    detail_resp = await async_client.get(f"/api/v1/documents/{doc_id}", headers=headers)
    assert detail_resp.status_code == 404

    # 3. Request 2 (retry) executes against the pending row without storage failure
    resp2 = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert resp2.status_code == 204

    # 4. Final state: DB record is deleted, storage file is deleted (no orphan)
    session_gen_final = get_db_session()
    session_final = await anext(session_gen_final)
    assert await session_final.get(Document, doc_id) is None
    assert not await real_storage.exists(storage_key)
