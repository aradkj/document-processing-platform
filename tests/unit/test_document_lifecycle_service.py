"""Unit tests for document download formatting and deletion lifecycle services."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.document import Document
from app.services.document import (
    DocumentDatabaseFinalizationError,
    DocumentDeletionError,
    DocumentNotFoundError,
    DocumentStorageDeletionError,
    delete_document,
    format_content_disposition,
    get_active_document_for_download,
    matches_if_none_match,
)
from app.storage.base import StorageBackend
from app.storage.exceptions import StorageDeleteError


def test_format_content_disposition_ascii() -> None:
    """Verify standard ASCII filename formatting."""
    header = format_content_disposition("invoice.pdf")
    assert header == "attachment; filename=\"invoice.pdf\"; filename*=UTF-8''invoice.pdf"


def test_format_content_disposition_spaces_and_parentheses() -> None:
    """Verify filenames with spaces and parentheses are handled properly."""
    header = format_content_disposition("quarterly report (2026).pdf")
    assert 'filename="quarterly report (2026).pdf"' in header
    assert "filename*=UTF-8''quarterly%20report%20%282026%29.pdf" in header


def test_format_content_disposition_persian_unicode() -> None:
    """Verify Persian Unicode filenames use safe ASCII fallback and RFC 5987 UTF-8 encoding."""
    header = format_content_disposition("فاکتور_خرید.pdf")
    assert 'filename="document.pdf"' in header
    expected_utf8 = "%D9%81%D8%A7%DA%A9%D8%AA%D9%88%D8%B1_%D8%AE%D8%B1%DB%8C%D8%AF.pdf"
    assert f"filename*=UTF-8''{expected_utf8}" in header


def test_format_content_disposition_crlf_injection_protection() -> None:
    """Verify CRLF and newline characters are stripped to prevent header injection."""
    header = format_content_disposition("evil\r\nX-Injected: true\n.pdf")
    assert "\r" not in header
    assert "\n" not in header
    assert "evilX-Injected: true.pdf" in header or "evilX-Injected" in header


def test_format_content_disposition_path_separators_stripped() -> None:
    """Verify path separators / and \\ are stripped from header."""
    header = format_content_disposition("../../etc/passwd.pdf")
    assert "/" not in header.split('filename="')[1].split('"')[0]
    assert "\\" not in header


def test_format_content_disposition_empty_fallback() -> None:
    """Verify empty or whitespace-only filename falls back to 'document'."""
    header = format_content_disposition("   ")
    assert 'filename="document"' in header


def test_matches_if_none_match_exact_strong() -> None:
    """Verify exact strong ETag matches."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match(f'"{sha}"', f'"{sha}"') is True


def test_matches_if_none_match_non_matching() -> None:
    """Verify different ETag does not match."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match('"different_etag"', f'"{sha}"') is False


def test_matches_if_none_match_wildcard() -> None:
    """Verify wildcard '*' matches any existing representation."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match("*", f'"{sha}"') is True


def test_matches_if_none_match_weak_tag_comparison() -> None:
    """Verify RFC 9110 weak comparison: W/ prefix matches strong entity tag."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match(f'W/"{sha}"', f'"{sha}"') is True


def test_matches_if_none_match_multiple_etags_with_match() -> None:
    """Verify list of comma-separated ETags matches if one matches."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match(f'"tag1", "{sha}", "tag2"', f'"{sha}"') is True


def test_matches_if_none_match_multiple_etags_no_match() -> None:
    """Verify list of ETags without matching tag returns False."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match('"tag1", "tag2", "tag3"', f'"{sha}"') is False


def test_matches_if_none_match_commas_inside_quotes() -> None:
    """Verify tags containing commas inside quotes are not broken by comma splitting."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match(f'"odd,tag,with,commas", "{sha}"', f'"{sha}"') is True


def test_matches_if_none_match_malformed_input_safe_fallback() -> None:
    """Verify malformed inputs return False safely without unhandled exceptions."""
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert matches_if_none_match(None, f'"{sha}"') is False
    assert matches_if_none_match("", f'"{sha}"') is False
    assert matches_if_none_match("   ", f'"{sha}"') is False
    assert matches_if_none_match("malformed-unquoted", f'"{sha}"') is False
    assert matches_if_none_match('W/"unclosed-quote', f'"{sha}"') is False


@pytest.mark.asyncio
async def test_get_active_document_for_download_active() -> None:
    """Verify active document (deletion_pending_at is None) is returned."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="0123456789abcdef0123456789abcdef",
        original_filename="sample.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        deletion_pending_at=None,
    )

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_doc
    mock_session.execute.return_value = mock_result

    result = await get_active_document_for_download(
        mock_session,
        document_id=doc_id,
        user_id=user_id,
    )
    assert result is mock_doc


@pytest.mark.asyncio
async def test_get_active_document_for_download_not_found_or_pending() -> None:
    """Verify get_active_document_for_download returns None when query matches nothing."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    result = await get_active_document_for_download(
        mock_session,
        document_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_delete_document_success() -> None:
    """Verify happy path: active doc -> marked pending -> storage deleted -> row deleted."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    storage_key = "0123456789abcdef0123456789abcdef"
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key=storage_key,
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="b" * 64,
        deletion_pending_at=None,
    )

    mock_session = AsyncMock()
    # Step 1 lookup returns mock_doc
    res1 = MagicMock()
    res1.scalar_one_or_none.return_value = mock_doc
    # Step 4 lookup returns mock_doc for delete
    res4 = MagicMock()
    res4.scalar_one_or_none.return_value = mock_doc
    mock_session.execute.side_effect = [res1, res4]

    mock_storage = AsyncMock(spec=StorageBackend)
    mock_storage.delete.return_value = True

    result = await delete_document(
        document_id=doc_id,
        user_id=user_id,
        session=mock_session,
        storage=mock_storage,
    )

    assert result is True
    # Verify deletion intent was set
    assert mock_doc.deletion_pending_at is not None
    # Verify storage.delete called with missing_ok=True
    mock_storage.delete.assert_awaited_once_with(storage_key, missing_ok=True)
    # Verify session.delete called for mock_doc
    mock_session.delete.assert_awaited_once_with(mock_doc)


@pytest.mark.asyncio
async def test_delete_document_not_found() -> None:
    """Verify delete_document raises DocumentNotFoundError when document does not exist."""
    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_res
    mock_storage = AsyncMock(spec=StorageBackend)

    with pytest.raises(DocumentNotFoundError):
        await delete_document(
            document_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session=mock_session,
            storage=mock_storage,
        )

    mock_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_document_intent_commit_failure() -> None:
    """Verify failure during intent commit rolls back and does not touch storage."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="0123456789abcdef0123456789abcdef",
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="c" * 64,
        deletion_pending_at=None,
    )

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_doc
    mock_session.execute.return_value = mock_res
    mock_session.commit.side_effect = RuntimeError("DB Commit Failure")

    mock_storage = AsyncMock(spec=StorageBackend)

    with pytest.raises(DocumentDeletionError):
        await delete_document(
            document_id=doc_id,
            user_id=user_id,
            session=mock_session,
            storage=mock_storage,
        )

    mock_session.rollback.assert_awaited_once()
    mock_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_document_storage_failure_preserves_pending_row() -> None:
    """Verify storage failure raises DocumentStorageDeletionError and does not delete DB row."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="0123456789abcdef0123456789abcdef",
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="d" * 64,
        deletion_pending_at=None,
    )

    mock_session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_doc
    mock_session.execute.return_value = mock_res

    mock_storage = AsyncMock(spec=StorageBackend)
    mock_storage.delete.side_effect = StorageDeleteError("Filesystem I/O error")

    with pytest.raises(DocumentStorageDeletionError):
        await delete_document(
            document_id=doc_id,
            user_id=user_id,
            session=mock_session,
            storage=mock_storage,
        )

    # Document was marked pending
    assert mock_doc.deletion_pending_at is not None
    # DB row was NOT deleted
    mock_session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_document_retry_already_pending() -> None:
    """Verify retry of already pending document resumes and finalizes deletion."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    storage_key = "0123456789abcdef0123456789abcdef"
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key=storage_key,
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="e" * 64,
        deletion_pending_at=datetime.now(UTC),  # Already pending
    )

    mock_session = AsyncMock()
    res1 = MagicMock()
    res1.scalar_one_or_none.return_value = mock_doc
    res4 = MagicMock()
    res4.scalar_one_or_none.return_value = mock_doc
    mock_session.execute.side_effect = [res1, res4]

    mock_storage = AsyncMock(spec=StorageBackend)
    mock_storage.delete.return_value = False  # already missing

    result = await delete_document(
        document_id=doc_id,
        user_id=user_id,
        session=mock_session,
        storage=mock_storage,
    )

    assert result is True
    mock_storage.delete.assert_awaited_once_with(storage_key, missing_ok=True)
    mock_session.delete.assert_awaited_once_with(mock_doc)


@pytest.mark.asyncio
async def test_delete_document_final_db_failure_raises() -> None:
    """Verify DB failure during final row deletion raises DocumentDatabaseFinalizationError."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="0123456789abcdef0123456789abcdef",
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="f" * 64,
        deletion_pending_at=datetime.now(UTC),
    )

    mock_session = AsyncMock()
    res1 = MagicMock()
    res1.scalar_one_or_none.return_value = mock_doc
    res4 = MagicMock()
    res4.scalar_one_or_none.return_value = mock_doc
    # On verification check, row still exists
    res_check = MagicMock()
    res_check.scalar_one_or_none.return_value = doc_id

    mock_session.execute.side_effect = [res1, res4, res_check]
    # Commit succeeds for step 2 (releasing lock), fails on final step 4
    mock_session.commit.side_effect = [None, RuntimeError("Final DB Commit Failed")]

    mock_storage = AsyncMock(spec=StorageBackend)
    mock_storage.delete.return_value = True

    with pytest.raises(DocumentDatabaseFinalizationError):
        await delete_document(
            document_id=doc_id,
            user_id=user_id,
            session=mock_session,
            storage=mock_storage,
        )


@pytest.mark.asyncio
async def test_delete_document_cancellation_during_final_commit() -> None:
    """Verify cancellation during final commit checks DB state and re-raises CancelledError."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="0123456789abcdef0123456789abcdef",
        original_filename="test.pdf",
        content_type="application/pdf",
        size_bytes=100,
        sha256="0" * 64,
        deletion_pending_at=datetime.now(UTC),
    )

    mock_session = AsyncMock()
    res1 = MagicMock()
    res1.scalar_one_or_none.return_value = mock_doc
    res4 = MagicMock()
    res4.scalar_one_or_none.return_value = mock_doc
    res_check = MagicMock()
    res_check.scalar_one_or_none.return_value = doc_id

    mock_session.execute.side_effect = [res1, res4, res_check]
    mock_session.commit.side_effect = [None, asyncio.CancelledError()]

    mock_storage = AsyncMock(spec=StorageBackend)

    with pytest.raises(asyncio.CancelledError):
        await delete_document(
            document_id=doc_id,
            user_id=user_id,
            session=mock_session,
            storage=mock_storage,
        )
