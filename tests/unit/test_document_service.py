"""Unit tests for document upload service streaming, size limits, and compensation."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

from app.db.models.user import User
from app.services.document import (
    DocumentPersistenceError,
    DocumentStorageError,
    UploadSizeExceededError,
    upload_document,
)
from app.services.document_validation import EmptyFileError, ExtensionMismatchError
from app.storage.base import StorageBackend
from app.storage.exceptions import StorageWriteError
from app.storage.types import StoredObject


@pytest.fixture
def mock_user() -> User:
    """Fixture providing a mock authenticated User."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    return user


@pytest.fixture
def mock_storage() -> MagicMock:
    """Fixture providing a mock StorageBackend."""
    storage = MagicMock(spec=StorageBackend)
    storage.generate_key.return_value = "a" * 32
    return storage


@pytest.fixture
def mock_session() -> AsyncMock:
    """Fixture providing a mock AsyncSession."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.mark.asyncio
async def test_upload_document_success(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify successful upload stream, storage persistence, and document creation."""
    pdf_content = b"%PDF-1.7\r\nTest content for document service upload testing."
    expected_sha256 = hashlib.sha256(pdf_content).hexdigest()
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="test_document.pdf",
    )

    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key="a" * 32,
            size_bytes=len(pdf_content),
            checksum_sha256=expected_sha256,
            created_at=MagicMock(),
        )
    )

    doc = await upload_document(
        file=file_obj,
        user=mock_user,
        session=mock_session,
        storage=mock_storage,
        max_upload_size=1048576,
        chunk_size=1024,
    )

    assert doc.original_filename == "test_document.pdf"
    assert doc.content_type == "application/pdf"
    assert doc.size_bytes == len(pdf_content)
    assert doc.sha256 == expected_sha256
    assert doc.user_id == mock_user.id
    assert doc.storage_key == "a" * 32

    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()
    mock_session.refresh.assert_called_once_with(doc)


@pytest.mark.asyncio
async def test_upload_document_empty_file_rejected(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify zero-byte upload is rejected before writing to storage."""
    file_obj = UploadFile(
        file=io.BytesIO(b""),
        filename="empty.pdf",
    )

    with pytest.raises(EmptyFileError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.save.assert_not_called()
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upload_document_size_exceeded_in_initial_header(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify upload exceeding maximum size in initial header chunk is rejected immediately."""
    pdf_content = b"%PDF-1.7\r\n" + b"x" * 200
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="large.pdf",
    )

    # Set max size smaller than header
    with pytest.raises(UploadSizeExceededError) as exc_info:
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=50,  # 50 bytes max limit
            chunk_size=1024,
        )

    assert "exceeds maximum allowed upload size" in str(exc_info.value)
    mock_storage.save.assert_not_called()
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upload_document_size_exceeded_during_streaming(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify stream is aborted during chunk iteration when size limit is crossed."""
    header = b"%PDF-1.7\r\n"
    body_chunks = [b"a" * 100, b"b" * 100, b"c" * 100]
    full_content = header + b"".join(body_chunks)

    file_obj = UploadFile(
        file=io.BytesIO(full_content),
        filename="stream_limit.pdf",
    )

    # Implement real consumption in mock_storage.save
    async def fake_save(
        stream: Any,
        *,
        storage_key: str | None = None,
    ) -> StoredObject:
        async for _chunk in stream:
            pass
        return StoredObject(
            storage_key="a" * 32,
            size_bytes=len(full_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )

    mock_storage.save = AsyncMock(side_effect=fake_save)

    # Set max limit to 150 bytes (header + chunk 1 is ~110 bytes, chunk 2 crosses 210 bytes)
    with pytest.raises(UploadSizeExceededError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=150,
            chunk_size=100,
        )

    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upload_document_extension_mismatch_rejected(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify extension mismatch raises ExtensionMismatchError before saving to storage."""
    # PNG content disguised as .pdf
    png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."
    file_obj = UploadFile(
        file=io.BytesIO(png_content),
        filename="fake.pdf",
    )

    with pytest.raises(ExtensionMismatchError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.save.assert_not_called()
    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upload_document_storage_error_mapping(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify StorageError during save is wrapped as DocumentStorageError."""
    pdf_content = b"%PDF-1.7\r\nValid PDF"
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="valid.pdf",
    )

    mock_storage.save = AsyncMock(side_effect=StorageWriteError("Disk full"))

    with pytest.raises(DocumentStorageError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_upload_document_database_failure_triggers_compensation(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify that if database commit fails, the stored object is deleted for compensation."""
    pdf_content = b"%PDF-1.7\r\nValid PDF content"
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="compensation.pdf",
    )

    target_key = "b" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="fake_sha256",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock(return_value=True)

    # Simulate database commit failure (e.g. database disconnect)
    mock_session.commit.side_effect = RuntimeError("Database connection terminated")

    with pytest.raises(DocumentPersistenceError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    # Verify rollback was called on database session
    mock_session.rollback.assert_called_once()
    # Verify compensation deletion was triggered on storage with exact storage_key
    mock_storage.delete.assert_called_once_with(target_key, missing_ok=True)


# ===========================================================================
# Cancellation Safety Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_upload_cancellation_before_storage_write(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation before storage writing cleans up file and does not call storage."""
    file_obj = MagicMock(spec=UploadFile)
    file_obj.filename = "test.pdf"
    file_obj.read = AsyncMock(side_effect=asyncio.CancelledError("Client disconnected before read"))
    file_obj.close = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.save.assert_not_called()
    mock_storage.delete.assert_not_called()
    mock_session.add.assert_not_called()
    file_obj.close.assert_called_once()


@pytest.mark.asyncio
async def test_upload_cancellation_during_streaming(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation during chunk streaming aborts and guarantees file close."""
    pdf_content = b"%PDF-1.7\r\n" + b"x" * 2000
    file_obj = UploadFile(
        file=io.BytesIO(pdf_content),
        filename="test.pdf",
    )

    async def cancelling_save(stream: Any, *, storage_key: str | None = None) -> StoredObject:
        async for _ in stream:
            raise asyncio.CancelledError("Client disconnected mid-stream")
        raise RuntimeError("Unreachable")

    mock_storage.save = AsyncMock(side_effect=cancelling_save)
    mock_storage.delete = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.delete.assert_not_called()
    mock_session.add.assert_not_called()
    assert file_obj.file.closed


@pytest.mark.asyncio
async def test_upload_cancellation_before_commit_triggers_cleanup(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation after storage publication but before commit deletes storage object."""
    pdf_content = b"%PDF-1.7\r\nContent"
    file_obj = UploadFile(file=io.BytesIO(pdf_content), filename="test.pdf")

    target_key = "c" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock(return_value=True)

    # Simulate cancellation when session.add is called (before commit)
    mock_session.add.side_effect = asyncio.CancelledError("Client aborted before commit")

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_session.rollback.assert_called_once()
    mock_storage.delete.assert_called_once_with(target_key, missing_ok=True)
    assert file_obj.file.closed


@pytest.mark.asyncio
async def test_upload_cancellation_during_commit_uncommitted_triggers_cleanup(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation during commit when row was not committed unlinks storage object."""
    pdf_content = b"%PDF-1.7\r\nContent"
    file_obj = UploadFile(file=io.BytesIO(pdf_content), filename="test.pdf")

    target_key = "d" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock(return_value=True)

    mock_session.commit.side_effect = asyncio.CancelledError("Cancelled during commit")
    # Database query check returns None (not committed)
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_execute_result

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_session.rollback.assert_called_once()
    mock_storage.delete.assert_called_once_with(target_key, missing_ok=True)
    assert file_obj.file.closed


@pytest.mark.asyncio
async def test_upload_cancellation_during_commit_already_committed_preserves_storage(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation during commit when row WAS committed preserves storage object."""
    pdf_content = b"%PDF-1.7\r\nContent"
    file_obj = UploadFile(file=io.BytesIO(pdf_content), filename="test.pdf")

    target_key = "e" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock()

    mock_session.commit.side_effect = asyncio.CancelledError("Cancelled during commit")
    # Database query check finds the document committed in PostgreSQL
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = uuid.uuid4()
    mock_session.execute.return_value = mock_execute_result

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    # Must NOT delete storage object because document exists in PostgreSQL!
    mock_storage.delete.assert_not_called()
    assert file_obj.file.closed


@pytest.mark.asyncio
async def test_upload_cancellation_after_commit_preserves_storage(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify cancellation after commit (e.g. during refresh) preserves storage object."""
    pdf_content = b"%PDF-1.7\r\nContent"
    file_obj = UploadFile(file=io.BytesIO(pdf_content), filename="test.pdf")

    target_key = "f" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock()

    # Commit succeeds, but refresh is cancelled
    mock_session.refresh.side_effect = asyncio.CancelledError("Cancelled during refresh")

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.delete.assert_not_called()
    assert file_obj.file.closed


@pytest.mark.asyncio
async def test_upload_cancellation_cleanup_failure_does_not_mask_cancelled_error(
    mock_user: User,
    mock_storage: MagicMock,
    mock_session: AsyncMock,
) -> None:
    """Verify that storage delete failure during cleanup preserves CancelledError."""
    pdf_content = b"%PDF-1.7\r\nContent"
    file_obj = UploadFile(file=io.BytesIO(pdf_content), filename="test.pdf")

    target_key = "g" * 32
    mock_storage.save = AsyncMock(
        return_value=StoredObject(
            storage_key=target_key,
            size_bytes=len(pdf_content),
            checksum_sha256="abc",
            created_at=MagicMock(),
        )
    )
    mock_storage.delete = AsyncMock(side_effect=RuntimeError("Storage disk offline"))
    mock_session.add.side_effect = asyncio.CancelledError("Cancelled before commit")

    with pytest.raises(asyncio.CancelledError):
        await upload_document(
            file=file_obj,
            user=mock_user,
            session=mock_session,
            storage=mock_storage,
            max_upload_size=1048576,
            chunk_size=1024,
        )

    mock_storage.delete.assert_called_once_with(target_key, missing_ok=True)
    assert file_obj.file.closed
