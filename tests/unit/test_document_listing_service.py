"""Unit tests for document listing, sorting, filtering, escaping, and detail retrieval service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.document import Document
from app.services.document import (
    escape_like_wildcards,
    get_document_by_id,
    list_documents,
)


def test_escape_like_wildcards_standard_text() -> None:
    """Verify standard text without wildcards remains unchanged."""
    assert escape_like_wildcards("invoice") == "invoice"
    assert escape_like_wildcards("quarterly report 2026") == "quarterly report 2026"


def test_escape_like_wildcards_percent() -> None:
    """Verify percent character is escaped."""
    assert escape_like_wildcards("100%") == "100\\%"
    assert escape_like_wildcards("%discount%") == "\\%discount\\%"


def test_escape_like_wildcards_underscore() -> None:
    """Verify underscore character is escaped."""
    assert escape_like_wildcards("doc_1") == "doc\\_1"
    assert escape_like_wildcards("_test_") == "\\_test\\_"


def test_escape_like_wildcards_escape_character() -> None:
    """Verify backslash character itself is doubled."""
    assert escape_like_wildcards("path\\name") == "path\\\\name"


def test_escape_like_wildcards_combined() -> None:
    """Verify combination of wildcards and backslashes are all escaped correctly."""
    assert escape_like_wildcards("100%_file\\test") == "100\\%\\_file\\\\test"


@pytest.mark.asyncio
async def test_get_document_by_id_owner_match() -> None:
    """Verify get_document_by_id returns document when ID and user_id match."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    expected_doc = MagicMock(spec=Document)
    expected_doc.id = doc_id
    expected_doc.user_id = user_id

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = expected_doc
    mock_session.execute.return_value = mock_result

    result = await get_document_by_id(mock_session, document_id=doc_id, user_id=user_id)

    assert result is expected_doc
    mock_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_document_by_id_not_found() -> None:
    """Verify get_document_by_id returns None when document is not found or not owned."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_result

    result = await get_document_by_id(mock_session, document_id=doc_id, user_id=user_id)

    assert result is None
    mock_session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_documents_empty_count() -> None:
    """Verify list_documents short-circuits when total count is zero."""
    user_id = uuid.uuid4()

    mock_session = AsyncMock()
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 0
    mock_session.execute.return_value = mock_count_result

    items, total = await list_documents(mock_session, user_id=user_id)

    assert items == []
    assert total == 0
    # Exactly one query (the count query) must be executed when total is 0
    assert mock_session.execute.await_count == 1


@pytest.mark.asyncio
async def test_list_documents_populated() -> None:
    """Verify list_documents executes count and query when records exist."""
    user_id = uuid.uuid4()
    mock_doc1 = MagicMock(spec=Document)
    mock_doc2 = MagicMock(spec=Document)

    mock_session = AsyncMock()
    mock_count_result = MagicMock()
    mock_count_result.scalar_one.return_value = 2

    mock_query_result = MagicMock()
    mock_query_result.scalars.return_value.all.return_value = [mock_doc1, mock_doc2]

    mock_session.execute.side_effect = [mock_count_result, mock_query_result]

    items, total = await list_documents(
        mock_session,
        user_id=user_id,
        limit=10,
        offset=0,
        sort_by="created_at",
        sort_order="desc",
        content_type="application/pdf",
        created_after=datetime(2026, 1, 1, tzinfo=UTC),
        created_before=datetime(2026, 12, 31, tzinfo=UTC),
        q="report",
    )

    assert items == [mock_doc1, mock_doc2]
    assert total == 2
    assert mock_session.execute.await_count == 2
