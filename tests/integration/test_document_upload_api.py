"""Integration tests for Secure Document Upload API (POST /api/v1/documents).

Tests execute against live PostgreSQL and local StorageBackend.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import secrets
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile
from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.db.models.document import Document
from app.db.models.user import User
from app.db.session import get_db_session
from app.main import app
from app.services.document import upload_document
from app.storage import get_default_storage, get_storage
from app.storage.base import StorageBackend
from app.storage.exceptions import StorageWriteError
from tests.integration.test_database import is_postgres_available


@pytest.fixture
async def sample_user() -> AsyncGenerator[User, None]:
    """Create and persist an isolated test user in PostgreSQL."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    user_id = uuid.uuid4()
    unique_suffix = secrets.token_hex(4)
    user = User(
        id=user_id,
        email=f"upload_user_{unique_suffix}@example.com",
        username=f"upload_{unique_suffix}",
        password_hash="mock_argon2id_hash_for_upload_test",
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    try:
        yield user
    finally:
        # Cleanup user and any associated documents
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


# =====================================================================
# Authentication & Authorization Integration Tests
# =====================================================================


@pytest.mark.asyncio
async def test_upload_anonymous_rejected(async_client: AsyncClient) -> None:
    """Verify anonymous request without Authorization header returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    files = {"file": ("report.pdf", b"%PDF-1.7\r\nSample content", "application/pdf")}
    resp = await async_client.post("/api/v1/documents", files=files)

    assert resp.status_code == 401
    assert "not authenticated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_invalid_token_rejected(async_client: AsyncClient) -> None:
    """Verify request with invalid/malformed Bearer token returns 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    files = {"file": ("report.pdf", b"%PDF-1.7\r\nSample content", "application/pdf")}
    headers = {"Authorization": "Bearer invalid.jwt.token"}
    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_authenticated_pdf_success(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify authenticated user can upload a valid PDF document."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    pdf_content = b"%PDF-1.7\r\n%Binary stream verification\r\nHello World Document Content!"
    expected_sha256 = hashlib.sha256(pdf_content).hexdigest()
    expected_size = len(pdf_content)

    headers = get_auth_headers(sample_user)
    files = {"file": ("quarterly_report.pdf", pdf_content, "application/pdf")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 201

    data = resp.json()
    assert "id" in data
    assert data["original_filename"] == "quarterly_report.pdf"
    assert data["content_type"] == "application/pdf"
    assert data["size_bytes"] == expected_size
    assert data["sha256"] == expected_sha256
    assert "created_at" in data
    assert "updated_at" in data
    assert "storage_key" not in data  # Storage key must not be leaked

    doc_id = uuid.UUID(data["id"])

    # Verify database persistence
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        db_doc = await session.get(Document, doc_id)
        assert db_doc is not None
        assert db_doc.user_id == sample_user.id
        assert db_doc.original_filename == "quarterly_report.pdf"
        assert db_doc.content_type == "application/pdf"
        assert db_doc.size_bytes == expected_size
        assert db_doc.sha256 == expected_sha256
        assert len(db_doc.storage_key) == 32

        # Verify storage content matches original bytes bit-for-bit
        storage = get_default_storage()
        assert await storage.exists(db_doc.storage_key)
        async with await storage.open(db_doc.storage_key) as reader:
            stored_bytes = await reader.read()
            assert stored_bytes == pdf_content

        # Cleanup physical file
        await storage.delete(db_doc.storage_key, missing_ok=True)
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_upload_ownership_cannot_be_tampered(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify supplying a malicious user_id in multipart fields cannot override ownership."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    fake_user_id = str(uuid.uuid4())
    pdf_content = b"%PDF-1.7\r\nTamper Test Content"

    headers = get_auth_headers(sample_user)
    files = {"file": ("tamper_test.pdf", pdf_content, "application/pdf")}
    data = {"user_id": fake_user_id}

    resp = await async_client.post("/api/v1/documents", files=files, data=data, headers=headers)
    assert resp.status_code == 201

    doc_id = uuid.UUID(resp.json()["id"])

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        db_doc = await session.get(Document, doc_id)
        assert db_doc is not None
        # Must belong strictly to the authenticated user, NOT the supplied fake_user_id
        assert db_doc.user_id == sample_user.id
        assert str(db_doc.user_id) != fake_user_id

        storage = get_default_storage()
        await storage.delete(db_doc.storage_key, missing_ok=True)
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


# =====================================================================
# Content-Type & Format Integration Tests
# =====================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "expected_mime"),
    [
        ("sample.pdf", b"%PDF-1.4\r\nPDF Content", "application/pdf"),
        ("photo.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00JPEG Content", "image/jpeg"),
        ("photo.jpeg", b"\xff\xd8\xff\xe1\x00\x10Exif\x00JPEG Content", "image/jpeg"),
        ("graphic.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRPNG Content", "image/png"),
        ("scan_le.tiff", b"II*\x00\x08\x00\x00\x00TIFF Content", "image/tiff"),
        ("scan_be.tif", b"MM\x00*\x00\x00\x00\x08TIFF Content", "image/tiff"),
        ("modern.webp", b"RIFF\x20\x00\x00\x00WEBPVP8 WEBP Content", "image/webp"),
    ],
)
async def test_upload_supported_formats(
    async_client: AsyncClient,
    sample_user: User,
    filename: str,
    content: bytes,
    expected_mime: str,
) -> None:
    """Verify all allowed document and image formats are detected, validated, and stored."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    files = {"file": (filename, content, "application/octet-stream")}  # Intentionally generic MIME

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 201

    data = resp.json()
    assert data["content_type"] == expected_mime
    assert data["original_filename"] == filename

    # Cleanup storage
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        db_doc = await session.get(Document, uuid.UUID(data["id"]))
        if db_doc:
            storage = get_default_storage()
            await storage.delete(db_doc.storage_key, missing_ok=True)
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_upload_unsupported_format_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify unsupported file type (e.g. binary executable) is rejected with 415."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    files = {"file": ("malicious.exe", b"MZ\x90\x00\x03\x00\x00\x00", "application/x-dosexec")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 415
    assert "unsupported file format" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_extension_spoofing_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify file with PNG bytes disguised as .pdf is rejected with 415."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00..."
    files = {"file": ("fake_document.pdf", png_bytes, "application/pdf")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 415
    assert "does not match detected content type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_empty_file_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify empty 0-byte file upload returns 400 Bad Request."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    files = {"file": ("empty.pdf", b"", "application/pdf")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


# =====================================================================
# Filename Validation Integration Tests
# =====================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_filename",
    [
        "../path_traversal.pdf",
        "..\\windows_traversal.pdf",
        "nested/sub/file.pdf",
        "C:autoexec.bat.pdf",
        "has_null_byte%00.pdf",
        "no_extension",
    ],
)
async def test_upload_invalid_filenames_rejected(
    async_client: AsyncClient,
    sample_user: User,
    invalid_filename: str,
) -> None:
    """Verify unsafe filenames are rejected with 400 Bad Request."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    files = {"file": (invalid_filename, b"%PDF-1.7\r\nContent", "application/pdf")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_persian_filename_success(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify Persian / Unicode filename is preserved accurately in PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    persian_name = "گزارش_عملکرد_۱۴۰۳.pdf"
    headers = get_auth_headers(sample_user)
    files = {"file": (persian_name, b"%PDF-1.7\r\nPersian Document Bytes", "application/pdf")}

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["original_filename"] == persian_name

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        db_doc = await session.get(Document, uuid.UUID(resp.json()["id"]))
        assert db_doc is not None
        assert db_doc.original_filename == persian_name

        storage = get_default_storage()
        await storage.delete(db_doc.storage_key, missing_ok=True)
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


# =====================================================================
# Size Limit Enforcement Integration Tests
# =====================================================================


@pytest.mark.asyncio
async def test_upload_size_limit_exceeded_rejected(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify upload exceeding MAX_UPLOAD_SIZE_BYTES returns 413 and leaves no orphaned state."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Override max upload size to 256 bytes for this test
    custom_settings = Settings(_env_file=None, MAX_UPLOAD_SIZE_BYTES=256)
    app.dependency_overrides[get_settings] = lambda: custom_settings

    try:
        oversized_content = b"%PDF-1.7\r\n" + (b"X" * 1000)
        headers = get_auth_headers(sample_user)
        files = {"file": ("oversized.pdf", oversized_content, "application/pdf")}

        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 413
        assert "exceeds maximum allowed upload size" in resp.json()["detail"].lower()

        # Verify no document was persisted in database
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            res = await session.execute(
                select(Document).where(Document.original_filename == "oversized.pdf")
            )
            assert res.scalar_one_or_none() is None
        finally:
            try:
                await anext(session_gen)
            except StopAsyncIteration:
                pass
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_exact_size_limit_boundary_accepted(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify upload exactly at the configured maximum size limit succeeds."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # PDF header is 10 bytes: b"%PDF-1.7\r\n"
    # Pad to exactly 200 bytes
    exact_content = b"%PDF-1.7\r\n" + (b"0" * 190)
    assert len(exact_content) == 200

    custom_settings = Settings(_env_file=None, MAX_UPLOAD_SIZE_BYTES=200)
    app.dependency_overrides[get_settings] = lambda: custom_settings

    try:
        headers = get_auth_headers(sample_user)
        files = {"file": ("exact_limit.pdf", exact_content, "application/pdf")}

        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 201
        assert resp.json()["size_bytes"] == 200

        # Cleanup
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            db_doc = await session.get(Document, uuid.UUID(resp.json()["id"]))
            if db_doc:
                storage = get_default_storage()
                await storage.delete(db_doc.storage_key, missing_ok=True)
        finally:
            try:
                await anext(session_gen)
            except StopAsyncIteration:
                pass
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_one_byte_over_size_limit_rejected_413(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify upload exactly 1 byte over configured limit is rejected with 413."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    exact_content = b"%PDF-1.7\r\n" + (b"1" * 191)  # 201 bytes
    assert len(exact_content) == 201

    custom_settings = Settings(_env_file=None, MAX_UPLOAD_SIZE_BYTES=200)
    app.dependency_overrides[get_settings] = lambda: custom_settings

    try:
        headers = get_auth_headers(sample_user)
        files = {"file": ("one_byte_over.pdf", exact_content, "application/pdf")}

        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 413
        assert "exceeds maximum allowed upload size" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_midstream_size_limit_exceeded_rejected_413(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify upload exceeding limit mid-stream returns 413 without orphaned files."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Configure limit that passes initial 64KB buffer (e.g. 70,000 bytes) but fails at 100,000 bytes
    custom_settings = Settings(
        _env_file=None,
        MAX_UPLOAD_SIZE_BYTES=70000,
        STORAGE_CHUNK_SIZE=65536,
    )
    app.dependency_overrides[get_settings] = lambda: custom_settings

    # 100,000 bytes payload
    oversized_stream_content = b"%PDF-1.7\r\n" + (b"M" * (100000 - len(b"%PDF-1.7\r\n")))
    assert len(oversized_stream_content) == 100000

    try:
        headers = get_auth_headers(sample_user)
        files = {"file": ("midstream_oversized.pdf", oversized_stream_content, "application/pdf")}

        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 413
        assert "exceeds maximum allowed upload size" in resp.json()["detail"].lower()

        # Verify no document was persisted in PostgreSQL
        session_gen = get_db_session()
        session = await anext(session_gen)
        try:
            res = await session.execute(
                select(Document).where(Document.original_filename == "midstream_oversized.pdf")
            )
            assert res.scalar_one_or_none() is None
        finally:
            try:
                await anext(session_gen)
            except StopAsyncIteration:
                pass

        # Verify no temporary staging files remain in storage root
        storage = get_default_storage()
        if hasattr(storage, "root_path"):
            tmp_files = list(storage.root_path.rglob(".tmp_*"))
            assert tmp_files == []
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_missing_or_spoofed_content_length_header_still_enforced(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify server enforces actual streamed bytes regardless of spoofed Content-Length header."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    custom_settings = Settings(_env_file=None, MAX_UPLOAD_SIZE_BYTES=256)
    app.dependency_overrides[get_settings] = lambda: custom_settings

    try:
        oversized = b"%PDF-1.7\r\n" + (b"S" * 1000)
        headers = get_auth_headers(sample_user)
        # Attempt to spoof Content-Length
        headers["content-length"] = "100"
        files = {"file": ("spoofed_len.pdf", oversized, "application/pdf")}

        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 413
        assert "exceeds maximum allowed upload size" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_upload_genuine_storage_failure_returns_500(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify that genuine hardware/storage failure still maps to HTTP 500."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    pdf_content = b"%PDF-1.7\r\nHardware Storage Failure Test"
    headers = get_auth_headers(sample_user)
    files = {"file": ("disk_failure.pdf", pdf_content, "application/pdf")}

    mock_storage = MagicMock(spec=StorageBackend)
    mock_storage.generate_key.return_value = "f" * 32
    mock_storage.save = AsyncMock(
        side_effect=StorageWriteError("Hardware disk controller I/O failure")
    )

    app.dependency_overrides[get_storage] = lambda: mock_storage
    try:
        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 500
        assert "failed to process and store document upload" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_storage, None)


# =====================================================================
# Database Compensation & Concurrency Tests
# =====================================================================


@pytest.mark.asyncio
async def test_upload_database_failure_triggers_storage_compensation(
    async_client: AsyncClient,
    sample_user: User,
    monkeypatch: Any,
) -> None:
    """Verify that if database commit fails, the stored object is deleted for compensation."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    pdf_content = b"%PDF-1.7\r\nCompensation Test Content"
    headers = get_auth_headers(sample_user)
    files = {"file": ("compensation.pdf", pdf_content, "application/pdf")}

    # Track compensation deletion call
    deleted_keys: list[str] = []
    real_storage = get_default_storage()
    original_delete = real_storage.delete

    async def tracking_delete(key: str, *, missing_ok: bool = False) -> bool:
        deleted_keys.append(key)
        return await original_delete(key, missing_ok=missing_ok)

    monkeypatch.setattr(real_storage, "delete", tracking_delete)

    # Monkeypatch session.commit to fail
    from sqlalchemy.ext.asyncio import AsyncSession

    async def failing_commit(self: AsyncSession) -> None:
        raise RuntimeError("Simulated Database Crash During Commit")

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)

    resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
    assert resp.status_code == 500

    # Verify that storage deletion was called on the orphaned storage key
    assert len(deleted_keys) == 1
    orphaned_key = deleted_keys[0]
    assert len(orphaned_key) == 32

    # Verify the physical object no longer exists on disk
    assert not await real_storage.exists(orphaned_key)


@pytest.mark.asyncio
async def test_upload_concurrent_uploads(
    async_client: AsyncClient,
    sample_user: User,
) -> None:
    """Verify concurrent uploads generate distinct storage keys and document IDs."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)

    async def upload_one(index: int) -> dict[str, Any]:
        content = f"%PDF-1.7\r\nConcurrent upload content #{index}".encode()
        files = {"file": (f"concurrent_{index}.pdf", content, "application/pdf")}
        resp = await async_client.post("/api/v1/documents", files=files, headers=headers)
        assert resp.status_code == 201
        return cast(dict[str, Any], resp.json())

    results = await asyncio.gather(*(upload_one(i) for i in range(3)))

    ids = [r["id"] for r in results]
    assert len(set(ids)) == 3  # All IDs are distinct

    # Verify in DB and cleanup storage
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        storage = get_default_storage()
        for doc_id_str in ids:
            db_doc = await session.get(Document, uuid.UUID(doc_id_str))
            assert db_doc is not None
            await storage.delete(db_doc.storage_key, missing_ok=True)
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_upload_cancellation_during_commit_unlinks_storage_object(
    sample_user: User,
    monkeypatch: Any,
) -> None:
    """Verify live cancellation during commit triggers compensation unlinking storage object."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    storage = get_default_storage()

    pdf_content = b"%PDF-1.7\r\nCancellation live test content"
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="cancel_live.pdf",
    )

    deleted_keys: list[str] = []
    original_delete = storage.delete

    async def tracking_delete(key: str, *, missing_ok: bool = False) -> bool:
        deleted_keys.append(key)
        return await original_delete(key, missing_ok=missing_ok)

    monkeypatch.setattr(storage, "delete", tracking_delete)

    from sqlalchemy.ext.asyncio import AsyncSession

    async def cancelling_commit(self: AsyncSession) -> None:
        raise asyncio.CancelledError("Client connection cancelled during commit")

    monkeypatch.setattr(AsyncSession, "commit", cancelling_commit)

    try:
        with pytest.raises(asyncio.CancelledError):
            await upload_document(
                file=file_obj,
                user=sample_user,
                session=session,
                storage=storage,
                max_upload_size=1048576,
                chunk_size=1024,
            )

        # Verify compensation deleted the published storage object
        assert len(deleted_keys) == 1
        storage_key = deleted_keys[0]
        assert not await storage.exists(storage_key)

        # Verify no document was persisted in PostgreSQL
        res = await session.execute(
            select(Document).where(Document.original_filename == "cancel_live.pdf")
        )
        assert res.scalar_one_or_none() is None

        # Verify file is closed
        assert file_obj.file.closed
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_upload_cancellation_after_commit_preserves_document_and_storage(
    sample_user: User,
    monkeypatch: Any,
) -> None:
    """Verify cancellation after commit preserves both database record and storage object."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    storage = get_default_storage()

    pdf_content = b"%PDF-1.7\r\nCancellation post-commit test content"
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="cancel_post_commit_live.pdf",
    )

    deleted_keys: list[str] = []
    original_delete = storage.delete

    async def tracking_delete(key: str, *, missing_ok: bool = False) -> bool:
        deleted_keys.append(key)
        return await original_delete(key, missing_ok=missing_ok)

    monkeypatch.setattr(storage, "delete", tracking_delete)

    from sqlalchemy.ext.asyncio import AsyncSession

    async def cancelling_refresh(self: AsyncSession, instance: Any) -> None:
        raise asyncio.CancelledError("Client disconnected after commit during refresh")

    monkeypatch.setattr(AsyncSession, "refresh", cancelling_refresh)

    persisted_doc: Document | None = None
    try:
        with pytest.raises(asyncio.CancelledError):
            await upload_document(
                file=file_obj,
                user=sample_user,
                session=session,
                storage=storage,
                max_upload_size=1048576,
                chunk_size=1024,
            )

        # Storage object MUST NOT be deleted
        assert deleted_keys == []

        # Document record MUST exist in PostgreSQL
        res = await session.execute(
            select(Document).where(Document.original_filename == "cancel_post_commit_live.pdf")
        )
        persisted_doc = res.scalar_one_or_none()
        assert persisted_doc is not None
        assert await storage.exists(persisted_doc.storage_key)
        assert file_obj.file.closed
    finally:
        if persisted_doc:
            await storage.delete(persisted_doc.storage_key, missing_ok=True)
            await session.delete(persisted_doc)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass
