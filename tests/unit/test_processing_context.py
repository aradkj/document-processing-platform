"""Unit tests for ProcessingContext contract and invariant enforcement."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import MagicMock

import pytest

from app.processing.context import ProcessingContext
from app.processing.exceptions import InvalidProcessingInputError
from app.processing.types import ProcessingLimits
from app.storage.base import StoredObjectReader
from app.storage.types import StorageMetadata


class DummyReader(StoredObjectReader):
    """Minimal StoredObjectReader implementation for unit tests."""

    def __init__(self, key: str = "0123456789abcdef0123456789abcdef") -> None:
        self._key = key

    @property
    def storage_key(self) -> str:
        return self._key

    @property
    def metadata(self) -> StorageMetadata:
        from datetime import UTC, datetime

        return StorageMetadata(
            storage_key=self._key,
            size_bytes=100,
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

    @property
    def closed(self) -> bool:
        return False

    async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        yield b"chunk"

    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        yield b"chunk"

    async def read(self, size: int = -1) -> bytes:
        return b"content"

    def read_sync(self, size: int = -1) -> bytes:
        return b"content"

    def close(self) -> None:
        pass


def test_valid_processing_context_construction() -> None:
    """Verify clean instantiation of ProcessingContext with valid inputs."""
    doc_id = uuid.uuid4()
    reader = DummyReader()
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    ctx = ProcessingContext(
        document_id=doc_id,
        original_filename="sample.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=sha.upper(),  # Should be normalized to lowercase
        reader=reader,
        metadata={"source": "api"},
    )

    assert ctx.document_id == doc_id
    assert ctx.original_filename == "sample.pdf"
    assert ctx.content_type == "application/pdf"
    assert ctx.size_bytes == 1024
    assert ctx.sha256 == sha.lower()
    assert ctx.reader is reader
    assert ctx.correlation_id is not None
    assert len(ctx.correlation_id) > 0
    assert ctx.metadata["source"] == "api"


def test_context_immutability() -> None:
    """Verify ProcessingContext and its metadata cannot be mutated."""
    doc_id = uuid.uuid4()
    reader = DummyReader()
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    ctx = ProcessingContext(
        document_id=doc_id,
        original_filename="sample.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        sha256=sha,
        reader=reader,
        metadata={"tag": "read-only"},
    )

    with pytest.raises(AttributeError):
        ctx.original_filename = "changed.pdf"  # type: ignore[misc]

    with pytest.raises(TypeError):
        ctx.metadata["new_key"] = "malicious"  # type: ignore[index]


def test_invalid_document_id_rejected() -> None:
    """Verify non-UUID document_id is rejected."""
    reader = DummyReader()
    with pytest.raises(InvalidProcessingInputError, match=r"must be a valid uuid\.UUID"):
        ProcessingContext(
            document_id="not-a-uuid",  # type: ignore[arg-type]
            original_filename="sample.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            reader=reader,
        )


@pytest.mark.parametrize("bad_name", ["", "   "])
def test_invalid_filename_rejected(bad_name: str) -> None:
    """Verify empty or whitespace filename is rejected."""
    reader = DummyReader()
    with pytest.raises(InvalidProcessingInputError, match="original_filename cannot be empty"):
        ProcessingContext(
            document_id=uuid.uuid4(),
            original_filename=bad_name,
            content_type="application/pdf",
            size_bytes=1024,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            reader=reader,
        )


@pytest.mark.parametrize("bad_mime", ["", "pdf", "no_slash"])
def test_invalid_content_type_rejected(bad_mime: str) -> None:
    """Verify invalid MIME content type is rejected."""
    reader = DummyReader()
    with pytest.raises(InvalidProcessingInputError, match="must be a valid MIME type"):
        ProcessingContext(
            document_id=uuid.uuid4(),
            original_filename="sample.pdf",
            content_type=bad_mime,
            size_bytes=1024,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            reader=reader,
        )


def test_negative_size_bytes_rejected() -> None:
    """Verify negative size_bytes is rejected."""
    reader = DummyReader()
    with pytest.raises(InvalidProcessingInputError, match="must be non-negative"):
        ProcessingContext(
            document_id=uuid.uuid4(),
            original_filename="sample.pdf",
            content_type="application/pdf",
            size_bytes=-10,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            reader=reader,
        )


@pytest.mark.parametrize("bad_sha", ["", "too_short", "g" * 64, "12345"])
def test_invalid_sha256_rejected(bad_sha: str) -> None:
    """Verify non-64 hex character sha256 digests are rejected."""
    reader = DummyReader()
    with pytest.raises(InvalidProcessingInputError, match="64-character hexadecimal digest"):
        ProcessingContext(
            document_id=uuid.uuid4(),
            original_filename="sample.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256=bad_sha,
            reader=reader,
        )


def test_invalid_reader_rejected() -> None:
    """Verify object not implementing StoredObjectReader is rejected."""
    with pytest.raises(InvalidProcessingInputError, match="must implement StoredObjectReader"):
        ProcessingContext(
            document_id=uuid.uuid4(),
            original_filename="sample.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            reader=MagicMock(),  # Not an instance of StoredObjectReader
        )


def test_is_size_exceeded() -> None:
    """Verify is_size_exceeded helper compares size against configured limits."""
    reader = DummyReader()
    limits = ProcessingLimits(max_input_size_bytes=1000)

    ctx_ok = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="sample.pdf",
        content_type="application/pdf",
        size_bytes=500,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        reader=reader,
        limits=limits,
    )
    assert not ctx_ok.is_size_exceeded()

    ctx_exceeded = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="sample.pdf",
        content_type="application/pdf",
        size_bytes=2000,
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        reader=reader,
        limits=limits,
    )
    assert ctx_exceeded.is_size_exceeded()
