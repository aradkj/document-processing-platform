"""Integration tests for Secure Document Download API.

Tests execute against live PostgreSQL and local StorageBackend:
- GET /api/v1/documents/{document_id}/download
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.document import Document
from app.db.models.user import User
from app.db.session import get_db_session
from app.main import app
from app.storage import get_default_storage, get_storage
from app.storage.base import StorageBackend, StoredObjectReader
from app.storage.exceptions import (
    StoragePermissionError,
)
from app.storage.types import StorageMetadata
from tests.integration.test_database import is_postgres_available


@pytest.fixture
async def sample_user() -> AsyncGenerator[User, None]:
    """Create and persist an isolated primary test user in PostgreSQL."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    suffix = secrets.token_hex(4)
    user = User(
        id=uuid.uuid4(),
        email=f"down_user_a_{suffix}@example.com",
        username=f"down_user_a_{suffix}",
        password_hash="mock_argon2id_hash_for_download_test",
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
        email=f"down_user_b_{suffix}@example.com",
        username=f"down_user_b_{suffix}",
        password_hash="mock_argon2id_hash_for_download_test",
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
    """Generate Bearer token authorization header."""
    token = create_access_token(subject=user.id, auth_version=user.auth_version)
    return {"Authorization": f"Bearer {token}"}


async def upload_test_doc(
    async_client: AsyncClient,
    user: User,
    filename: str = "sample.pdf",
    content: bytes = b"%PDF-1.7\r\nHello World Download Test Content",
    content_type: str = "application/pdf",
) -> dict[str, Any]:
    """Helper to upload a document via API and return response data."""
    headers = get_auth_headers(user)
    files = {"file": (filename, content, content_type)}
    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 201, f"Upload helper failed: {resp.text}"
    return cast(dict[str, Any], resp.json())


# =====================================================================
# Authentication & Authorization Tests
# =====================================================================


@pytest.mark.asyncio
async def test_download_unauthenticated_rejected(async_client: AsyncClient) -> None:
    """Verify request without Authorization header returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_id = uuid.uuid4()
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download")
    assert resp.status_code == 401
    assert "not authenticated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_download_invalid_token_rejected(async_client: AsyncClient) -> None:
    """Verify request with invalid JWT returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_id = uuid.uuid4()
    resp = await async_client.get(
        f"/api/v1/documents/{doc_id}/download",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_download_malformed_uuid_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify malformed non-UUID parameter returns 422 Unprocessable Content."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    resp = await async_client.get("/api/v1/documents/not-a-valid-uuid/download", headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_download_nonexistent_document(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify nonexistent document returns 404 Not Found."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    random_id = uuid.uuid4()
    resp = await async_client.get(f"/api/v1/documents/{random_id}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"


@pytest.mark.asyncio
async def test_download_foreign_owned_document_anti_enumeration(
    async_client: AsyncClient,
    sample_user: User,
    second_user: User,
) -> None:
    """Verify User B attempting to download User A's document returns identical 404."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user, "user_a_file.pdf")
    doc_id = doc_data["id"]

    # User B requests User A's document
    headers_b = get_auth_headers(second_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers_b)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"


# =====================================================================
# Content & Headers Verification Tests
# =====================================================================


@pytest.mark.asyncio
async def test_download_authenticated_pdf_success(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify full successful download with correct content and headers."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nSample Document Bytes For Verification"
    doc_data = await upload_test_doc(
        async_client, sample_user, filename="test_doc.pdf", content=payload
    )
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)

    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-length"] == str(len(payload))
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "private" in resp.headers["cache-control"]
    assert resp.headers["etag"] == f'"{hashlib.sha256(payload).hexdigest()}"'
    assert 'attachment; filename="test_doc.pdf"' in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_content_disposition_filenames(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify Content-Disposition filename encoding for ASCII, spaces, parentheses, and Unicode."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)

    # 1. Filename with spaces and parentheses
    doc1 = await upload_test_doc(
        async_client, sample_user, filename="Financial Report (Q3 2026).pdf"
    )
    resp1 = await async_client.get(f"/api/v1/documents/{doc1['id']}/download", headers=headers)
    assert resp1.status_code == 200
    cd1 = resp1.headers["content-disposition"]
    assert 'filename="Financial Report (Q3 2026).pdf"' in cd1
    assert "filename*=UTF-8''Financial%20Report%20%28Q3%202026%29.pdf" in cd1

    # 2. Persian filename
    doc2 = await upload_test_doc(async_client, sample_user, filename="گزارش_فروش.pdf")
    resp2 = await async_client.get(f"/api/v1/documents/{doc2['id']}/download", headers=headers)
    assert resp2.status_code == 200
    cd2 = resp2.headers["content-disposition"]
    assert 'filename="document.pdf"' in cd2
    assert "filename*=UTF-8''%DA%AF%D8%B2%D8%A7%D8%B1%D8%B4_%D9%81%D8%B1%D9%88%D8%B4.pdf" in cd2


@pytest.mark.asyncio
async def test_download_conditional_etag_if_none_match(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify conditional request with matching strong If-None-Match returns 304 Not Modified."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request payload"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = f'"{sha256}"'

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == f'"{sha256}"'


@pytest.mark.asyncio
async def test_download_if_none_match_weak_etag(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify RFC 9110 weak comparison: W/"etag" matches strong ETag and returns 304."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request weak etag"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = f'W/"{sha256}"'

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == f'"{sha256}"'


@pytest.mark.asyncio
async def test_download_if_none_match_wildcard(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify RFC 9110 wildcard '*' matches any existing document ETag and returns 304."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request wildcard etag"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = "*"

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == f'"{sha256}"'


@pytest.mark.asyncio
async def test_download_if_none_match_comma_separated_list(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify multiple comma-separated ETags match if any item matches (RFC 9110)."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request list etags"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = f'"unrelated1", W/"{sha256}", "unrelated2"'

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == f'"{sha256}"'


@pytest.mark.asyncio
async def test_download_if_none_match_commas_inside_quoted_tags(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify comma characters inside quoted ETags are parsed properly without splitting."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request quoted comma"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = f'"tag,with,commas", "{sha256}"'

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 304
    assert resp.content == b""
    assert resp.headers["etag"] == f'"{sha256}"'


@pytest.mark.asyncio
async def test_download_if_none_match_non_matching(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify non-matching If-None-Match proceeds to normal 200 download response."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request non-matching payload"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)
    headers["If-None-Match"] = '"non-matching-etag"'

    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.content == payload


@pytest.mark.asyncio
async def test_download_if_none_match_malformed_header_safe_fallback(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify malformed If-None-Match safely falls back to streaming 200 response without 500."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request malformed header payload"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)
    # Malformed headers: bare strings, dangling quotes, excess commas
    for malformed in ["unquoted_etag", '"""', ",,,", "W/unquoted"]:
        headers["If-None-Match"] = malformed
        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
        assert resp.status_code == 200
        assert resp.content == payload


@pytest.mark.asyncio
async def test_download_if_none_match_never_opens_storage_on_304(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify StorageBackend.open is NEVER called when 304 Not Modified is returned."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    payload = b"%PDF-1.7\r\nConditional request storage open check"
    doc_data = await upload_test_doc(async_client, sample_user, content=payload)
    doc_id = doc_data["id"]
    sha256 = hashlib.sha256(payload).hexdigest()

    real_storage = get_default_storage()
    with patch.object(real_storage, "open", wraps=real_storage.open) as spy_open:
        headers = get_auth_headers(sample_user)
        headers["If-None-Match"] = f'"{sha256}"'

        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
        assert resp.status_code == 304
        spy_open.assert_not_called()


@pytest.mark.asyncio
async def test_download_pending_deletion_document_returns_404(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify document in pending-deletion state returns 404 (hidden from download)."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    # Mark document pending deletion in database
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    from datetime import UTC, datetime

    doc.deletion_pending_at = datetime.now(UTC)
    await session.commit()

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"


# =====================================================================
# Storage Error & Isolation Tests
# =====================================================================


@pytest.mark.asyncio
async def test_download_storage_missing_object_error(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify missing storage object returns generic 500 without leaking path or key."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    # Delete physical file from storage directly to simulate corruption/missing file
    storage = get_default_storage()
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, uuid.UUID(doc_id))
    assert doc is not None
    await storage.delete(doc.storage_key, missing_ok=True)

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)

    assert resp.status_code == 500
    assert "temporarily unavailable" in resp.json()["detail"].lower()
    # Confirm no key or filesystem path leaked
    assert doc.storage_key not in resp.text
    assert "c:" not in resp.text.lower()
    assert "/" not in resp.json()["detail"]


@pytest.mark.asyncio
async def test_download_no_storage_access_before_ownership_check(
    async_client: AsyncClient,
    sample_user: User,
    second_user: User,
) -> None:
    """Verify StorageBackend.open is NEVER invoked when foreign document is requested."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    # Mock storage backend
    storage_mock = AsyncMock(spec=StorageBackend)
    app.dependency_overrides[get_storage] = lambda: storage_mock

    try:
        headers_b = get_auth_headers(second_user)
        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers_b)
        assert resp.status_code == 404

        # Verify storage was never touched
        storage_mock.open.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.mark.asyncio
async def test_download_bounded_streaming_multi_chunk(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify bounded memory streaming for multi-chunk documents."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # 1MB payload (exceeds default 64KB chunk size, producing multiple streaming chunks)
    pdf_header = b"%PDF-1.7\r\n"
    chunk_pattern = b"0123456789abcdef" * 4096  # 64KB
    payload = pdf_header + (chunk_pattern * 16)  # ~1MB
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    doc_data = await upload_test_doc(
        async_client, sample_user, filename="large_report.pdf", content=payload
    )
    doc_id = doc_data["id"]

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)

    assert resp.status_code == 200
    assert len(resp.content) == len(payload)
    assert hashlib.sha256(resp.content).hexdigest() == expected_sha256
    assert resp.headers["content-length"] == str(len(payload))


@pytest.mark.asyncio
async def test_download_storage_permission_error_mapping(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify storage permission error maps to 500 without leaking details."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    storage_mock = AsyncMock(spec=StorageBackend)
    storage_mock.open.side_effect = StoragePermissionError("Permission denied opening storage key")
    app.dependency_overrides[get_storage] = lambda: storage_mock

    try:
        headers = get_auth_headers(sample_user)
        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
        assert resp.status_code == 500
        assert "failed to access document storage" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.mark.asyncio
async def test_download_reader_closure_guaranteed(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify stored object reader close() is always called on successful streaming."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    mock_reader = AsyncMock(spec=StoredObjectReader)
    mock_reader.closed = False

    async def fake_chunks(size: int | None = None) -> AsyncIterator[bytes]:
        yield b"%PDF-1.7\r\nChunk 1"
        yield b"Chunk 2"

    mock_reader.chunks = fake_chunks
    storage_mock = AsyncMock(spec=StorageBackend)
    storage_mock.open.return_value = mock_reader
    app.dependency_overrides[get_storage] = lambda: storage_mock

    try:
        headers = get_auth_headers(sample_user)
        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
        assert resp.status_code == 200
        assert b"%PDF-1.7" in resp.content
        mock_reader.close.assert_called_once()
    finally:
        app.dependency_overrides.pop(get_storage, None)


@pytest.mark.asyncio
async def test_download_no_storage_key_or_path_leakage(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify internal storage keys and file paths are never leaked in download responses."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = uuid.UUID(doc_data["id"])

    session_gen = get_db_session()
    session = await anext(session_gen)
    doc = await session.get(Document, doc_id)
    assert doc is not None
    storage_key = doc.storage_key

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)

    assert resp.status_code == 200
    for _header_name, header_value in resp.headers.items():
        assert storage_key not in header_value
        assert "docker" not in header_value.lower()
        assert "storage" not in header_value.lower()


@pytest.mark.asyncio
async def test_download_streaming_incremental_chunk_monitoring(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify endpoint streams chunks incrementally through reader.chunks."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_data = await upload_test_doc(async_client, sample_user)
    doc_id = doc_data["id"]

    chunk_sizes_yielded: list[int] = []
    real_storage = get_default_storage()
    original_open = real_storage.open

    class MonitoredReader(StoredObjectReader):
        def __init__(self, inner: StoredObjectReader) -> None:
            self._inner = inner

        @property
        def storage_key(self) -> str:
            return self._inner.storage_key

        @property
        def metadata(self) -> StorageMetadata:
            return self._inner.metadata

        @property
        def closed(self) -> bool:
            return self._inner.closed

        async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
            async for chunk in self._inner.chunks(chunk_size):
                chunk_sizes_yielded.append(len(chunk))
                yield chunk

        def sync_chunks(self, chunk_size: int | None = None) -> Any:
            return self._inner.sync_chunks(chunk_size)

        async def read(self, size: int = -1) -> bytes:
            return await self._inner.read(size)

        def read_sync(self, size: int = -1) -> bytes:
            return self._inner.read_sync(size)

        def close(self) -> None:
            self._inner.close()

    async def monitored_open(key: str) -> StoredObjectReader:
        real_reader = await original_open(key)
        return MonitoredReader(real_reader)

    with patch.object(real_storage, "open", side_effect=monitored_open):
        headers = get_auth_headers(sample_user)
        resp = await async_client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)
        assert resp.status_code == 200
        assert len(chunk_sizes_yielded) >= 1
        assert sum(chunk_sizes_yielded) == len(resp.content)
