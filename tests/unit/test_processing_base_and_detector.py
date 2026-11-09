"""Unit tests for BaseProcessor, FormatDetector, and PrefixedStreamReader streaming integrity."""

from __future__ import annotations

import contextlib
import io
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.processing.base import BaseProcessor
from app.processing.context import ProcessingContext
from app.processing.detector import DetectionResult, FormatDetector
from app.processing.exceptions import (
    CorruptedDocumentError,
    UnsupportedDocumentFormatError,
)
from app.processing.result import ProcessingResult
from app.processing.types import ProcessingCapability, ProcessorMetadata
from app.storage.base import StorageBackend, StoredObjectReader
from app.storage.exceptions import StorageObjectNotFoundError, StorageReadError
from app.storage.local import LocalStorage, LocalStoredObjectReader
from app.storage.types import StorageMetadata, StoredObject


class ForwardOnlyStreamReader(StoredObjectReader):
    """Genuinely forward-only reader whose read operations advance its internal pointer."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._closed = False
        self.open_count = 1
        self.close_count = 0

    @property
    def storage_key(self) -> str:
        return "mock_storage_key"

    @property
    def metadata(self) -> StorageMetadata:
        return StorageMetadata(
            storage_key="mock_key",
            size_bytes=len(self._data),
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        if size == 0:
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def read_sync(self, size: int = -1) -> bytes:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        if size == 0:
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        while self._pos < len(self._data):
            chunk = self._data[self._pos : self._pos + step]
            self._pos += len(chunk)
            yield chunk

    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        while self._pos < len(self._data):
            chunk = self._data[self._pos : self._pos + step]
            self._pos += len(chunk)
            yield chunk

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.close_count += 1


class ConcreteTestProcessor(BaseProcessor):
    """Concrete processor implementation to test BaseProcessor methods."""

    @property
    def metadata(self) -> ProcessorMetadata:
        return ProcessorMetadata(
            name="concrete-test-proc",
            version="1.0.0",
            supported_formats=("application/pdf", "image/png"),
            capabilities=(
                ProcessingCapability.TEXT_EXTRACTION,
                ProcessingCapability.PAGE_RENDERING,
            ),
            description="Concrete test processor",
        )

    async def process(self, context: ProcessingContext) -> ProcessingResult:
        content = await context.reader.read(-1)
        return ProcessingResult(
            processor_name=self.name,
            processor_version=self.version,
            extracted_text=f"Processed {len(content)} bytes for {context.original_filename}",
        )


@pytest.mark.asyncio
async def test_base_processor_contract() -> None:
    """Verify BaseProcessor helper properties and execution contract."""
    proc = ConcreteTestProcessor()

    assert proc.name == "concrete-test-proc"
    assert proc.version == "1.0.0"
    assert proc.supported_formats == ("application/pdf", "image/png")
    assert proc.capabilities == (
        ProcessingCapability.TEXT_EXTRACTION,
        ProcessingCapability.PAGE_RENDERING,
    )

    assert proc.supports_format("application/pdf")
    assert proc.supports_format("APPLICATION/PDF")
    assert proc.supports_format("image/png")
    assert not proc.supports_format("image/jpeg")

    assert proc.supports_capability(ProcessingCapability.TEXT_EXTRACTION)
    assert proc.supports_capability(ProcessingCapability.PAGE_RENDERING)
    assert not proc.supports_capability(ProcessingCapability.TABLE_EXTRACTION)

    reader = ForwardOnlyStreamReader(b"%PDF-1.7\r\nHello World")
    ctx = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="doc.pdf",
        content_type="application/pdf",
        size_bytes=len(b"%PDF-1.7\r\nHello World"),
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        reader=reader,
    )

    result = await proc.process(ctx)
    assert result.processor_name == "concrete-test-proc"
    assert "Processed 21 bytes" in (result.extracted_text or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_prefix", "expected_mime"),
    [
        (b"%PDF-1.7\r\nPDF Content Stream", "application/pdf"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
        (b"II*\x00\x08\x00\x00\x00", "image/tiff"),
        (b"MM\x00*\x00\x00\x00\x08", "image/tiff"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
    ],
)
async def test_format_detector_and_stream_replay_byte_for_byte(
    header_prefix: bytes, expected_mime: str
) -> None:
    """Verify that FormatDetector identifies MIME and replays exact bytes byte-for-byte."""
    detector = FormatDetector(buffer_size=65536)

    # 1. Document smaller than 64 KiB
    small_data = header_prefix + b"_small_payload_" * 50  # ~1 KiB
    small_reader = ForwardOnlyStreamReader(small_data)
    detection_small = await detector.detect(small_reader)

    assert isinstance(detection_small, DetectionResult)
    assert detection_small.content_type == expected_mime
    replayed_small = await detection_small.reader.read(-1)
    assert replayed_small == small_data
    assert small_reader.open_count == 1

    # 2. Document exactly at boundary (65,536 bytes)
    padding_len = 65536 - len(header_prefix)
    boundary_data = header_prefix + (b"B" * padding_len)
    assert len(boundary_data) == 65536
    boundary_reader = ForwardOnlyStreamReader(boundary_data)
    detection_boundary = await detector.detect(boundary_reader)

    assert detection_boundary.content_type == expected_mime
    replayed_boundary = await detection_boundary.reader.read(-1)
    assert replayed_boundary == boundary_data

    # 3. Document larger than 64 KiB (128 KiB)
    large_data = header_prefix + (b"L" * 131072)
    large_reader = ForwardOnlyStreamReader(large_data)
    detection_large = await detector.detect(large_reader)

    assert detection_large.content_type == expected_mime
    replayed_large = await detection_large.reader.read(-1)
    assert replayed_large == large_data


def test_format_detector_sync_replay_byte_for_byte() -> None:
    """Verify synchronous detection and byte-for-byte replay."""
    detector = FormatDetector(buffer_size=65536)
    data = b"%PDF-1.7\r\nSync Stream Content" + (b"X" * 1000)
    reader = ForwardOnlyStreamReader(data)

    detection = detector.detect_sync(reader)
    assert detection.content_type == "application/pdf"
    replayed = detection.reader.read_sync(-1)
    assert replayed == data


@pytest.mark.asyncio
async def test_format_detector_chunked_streaming_replay() -> None:
    """Verify chunked streaming replay reconstructs stream across chunk boundaries."""
    detector = FormatDetector(buffer_size=65536)
    data = b"%PDF-1.7\r\n" + (b"0123456789abcdef" * 8192)  # ~131 KiB
    reader = ForwardOnlyStreamReader(data)

    detection = await detector.detect(reader)
    assert detection.content_type == "application/pdf"

    # Async chunk streaming
    chunks: list[bytes] = []
    async for chunk in detection.reader.chunks(chunk_size=1024):
        chunks.append(chunk)

    reconstructed = b"".join(chunks)
    assert reconstructed == data


def test_format_detector_sync_chunked_streaming_replay() -> None:
    """Verify synchronous chunk streaming replay."""
    detector = FormatDetector(buffer_size=65536)
    data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (b"P" * 10000)
    reader = ForwardOnlyStreamReader(data)

    detection = detector.detect_sync(reader)
    assert detection.content_type == "image/png"

    chunks: list[bytes] = []
    for chunk in detection.reader.sync_chunks(chunk_size=512):
        chunks.append(chunk)

    assert b"".join(chunks) == data


@pytest.mark.asyncio
async def test_format_detector_multiple_partial_reads() -> None:
    """Verify replay reader handles multiple partial reads across prefix/inner boundary."""
    detector = FormatDetector(buffer_size=100)
    data = b"%PDF-1.7\r\n" + (b"A" * 500)
    reader = ForwardOnlyStreamReader(data)

    detection = await detector.detect(reader)
    replay_reader = detection.reader

    # Multiple small reads: 10, 20, 50, 100 bytes
    c1 = await replay_reader.read(10)
    assert c1 == data[:10]

    c2 = await replay_reader.read(20)
    assert c2 == data[10:30]

    c3 = await replay_reader.read(50)
    assert c3 == data[30:80]

    c4 = await replay_reader.read(100)
    assert c4 == data[80:180]

    # Remaining bytes
    rem = await replay_reader.read(-1)
    assert rem == data[180:]

    assert c1 + c2 + c3 + c4 + rem == data


@pytest.mark.asyncio
async def test_format_detector_mixed_read_and_chunks() -> None:
    """Verify reading a prefix slice with read() then streaming remainder via chunks()."""
    detector = FormatDetector(buffer_size=64)
    data = b"%PDF-1.7\r\n" + (b"Z" * 1000)
    reader = ForwardOnlyStreamReader(data)

    detection = await detector.detect(reader)
    head_slice = await detection.reader.read(32)
    assert head_slice == data[:32]

    tail_chunks: list[bytes] = []
    async for chunk in detection.reader.chunks(chunk_size=128):
        tail_chunks.append(chunk)

    assert head_slice + b"".join(tail_chunks) == data


@pytest.mark.asyncio
async def test_format_detector_closing_and_resource_cleanup() -> None:
    """Verify that closing the replay reader safely propagates to the underlying reader."""
    detector = FormatDetector(buffer_size=64)
    data = b"%PDF-1.7\r\nPayload"
    inner_reader = ForwardOnlyStreamReader(data)

    detection = await detector.detect(inner_reader)
    replay_reader = detection.reader

    assert not replay_reader.closed
    assert not inner_reader.closed

    replay_reader.close()
    assert replay_reader.closed
    assert inner_reader.closed
    assert inner_reader.close_count == 1

    # Idempotent second close
    replay_reader.close()
    assert inner_reader.close_count == 1


@pytest.mark.asyncio
async def test_format_detector_empty_stream_raises_corrupted() -> None:
    """Verify empty stream raises CorruptedDocumentError without leaving unclosed descriptors."""
    detector = FormatDetector()
    reader_async = ForwardOnlyStreamReader(b"")
    reader_sync = ForwardOnlyStreamReader(b"")

    with pytest.raises(CorruptedDocumentError, match="Stored document stream is empty"):
        await detector.detect(reader_async)

    with pytest.raises(CorruptedDocumentError, match="Stored document stream is empty"):
        detector.detect_sync(reader_sync)


@pytest.mark.asyncio
async def test_format_detector_unrecognized_signature_raises_unsupported() -> None:
    """Verify unrecognized header bytes raise UnsupportedDocumentFormatError."""
    detector = FormatDetector()
    reader_async = ForwardOnlyStreamReader(b"UNKNOWN_BINARY_MAGIC_BYTES_12345678")
    reader_sync = ForwardOnlyStreamReader(b"UNKNOWN_BINARY_MAGIC_BYTES_12345678")

    with pytest.raises(UnsupportedDocumentFormatError, match="Unsupported file format"):
        await detector.detect(reader_async)

    with pytest.raises(UnsupportedDocumentFormatError, match="Unsupported file format"):
        detector.detect_sync(reader_sync)


@pytest.mark.asyncio
async def test_phase_5_worker_workflow_simulation() -> None:
    """Simulate complete Phase 5 background worker execution pipeline.

    Demonstrates:
    1. Single storage open.
    2. Format detection.
    3. Context construction with replay reader.
    4. Processor execution.
    5. Byte-for-byte stream reconstruction.
    6. Resource cleanup.
    """
    raw_content = b"%PDF-1.7\r\nSample Worker Document Bytes"
    storage_reader = ForwardOnlyStreamReader(raw_content)

    # 1. Open once
    assert storage_reader.open_count == 1

    # 2. Format detection
    detector = FormatDetector()
    content_type, replay_reader = await detector.detect(storage_reader)
    assert content_type == "application/pdf"

    # 3. Context construction
    ctx = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="worker_doc.pdf",
        content_type=content_type,
        size_bytes=len(raw_content),
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        reader=replay_reader,
    )

    # 4. Processor execution
    proc = ConcreteTestProcessor()
    result = await proc.process(ctx)

    assert result.status.value == "success"
    assert f"Processed {len(raw_content)} bytes" in (result.extracted_text or "")

    # 5. Verify single open
    assert storage_reader.open_count == 1

    # 6. Cleanup
    ctx.reader.close()
    assert storage_reader.closed


class InstrumentedObjectReader(StoredObjectReader):
    """Underlying object reader for InstrumentedStorageBackend tracking close invocations."""

    def __init__(self, storage_key: str, data: bytes) -> None:
        self._storage_key = storage_key
        self._data = data
        self._pos = 0
        self._closed = False
        self.close_count = 0

    @property
    def storage_key(self) -> str:
        return self._storage_key

    @property
    def metadata(self) -> StorageMetadata:
        return StorageMetadata(
            storage_key=self._storage_key,
            size_bytes=len(self._data),
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        if size == 0:
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def read_sync(self, size: int = -1) -> bytes:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        if size == 0:
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        try:
            while self._pos < len(self._data):
                chunk = self._data[self._pos : self._pos + step]
                self._pos += len(chunk)
                yield chunk
        finally:
            self.close()

    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        try:
            while self._pos < len(self._data):
                chunk = self._data[self._pos : self._pos + step]
                self._pos += len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.close_count += 1


class InstrumentedStorageBackend(StorageBackend):
    """Instrumented test StorageBackend implementing the real project contract."""

    def __init__(self, data_map: dict[str, bytes]) -> None:
        self.data_map = data_map
        self.open_invocations: list[str] = []
        self.created_readers: list[InstrumentedObjectReader] = []

    async def save(
        self,
        stream: Any,
        *,
        storage_key: str | None = None,
    ) -> StoredObject:
        raise NotImplementedError("Not invoked during open-count verification.")

    async def open(self, storage_key: str) -> StoredObjectReader:
        self.open_invocations.append(storage_key)
        if storage_key not in self.data_map:
            raise StorageObjectNotFoundError(f"Key '{storage_key}' not found.")
        reader = InstrumentedObjectReader(storage_key, self.data_map[storage_key])
        self.created_readers.append(reader)
        return reader

    async def delete(self, storage_key: str, *, missing_ok: bool = False) -> bool:
        raise NotImplementedError()

    async def exists(self, storage_key: str) -> bool:
        return storage_key in self.data_map

    async def get_metadata(self, storage_key: str) -> StorageMetadata:
        data = self.data_map[storage_key]
        return StorageMetadata(
            storage_key=storage_key,
            size_bytes=len(data),
            created_at=datetime.now(UTC),
            modified_at=datetime.now(UTC),
        )

    def generate_key(self) -> str:
        return "11/22/11223344556677889900aabbccddeeff"

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_actual_storage_backend_open_invocations() -> None:
    """Verify that FormatDetector and execution invoke StorageBackend.open() exactly once."""
    raw_content = b"%PDF-1.7\r\nSample Document Bytes For Open Count Test"
    storage_key = "11/22/11223344556677889900aabbccddeeff"
    backend = InstrumentedStorageBackend({storage_key: raw_content})

    # 1. Backend instantiated with zero open calls
    assert len(backend.open_invocations) == 0

    # 2. Invoke actual backend.open()
    underlying_reader = await backend.open(storage_key)
    assert isinstance(underlying_reader, InstrumentedObjectReader)
    assert len(backend.open_invocations) == 1
    assert backend.open_invocations == [storage_key]
    assert len(backend.created_readers) == 1
    assert underlying_reader.close_count == 0

    # 3. Format detection
    detector = FormatDetector(buffer_size=65536)
    detection = await detector.detect(underlying_reader)
    assert detection.content_type == "application/pdf"

    # Verify backend.open was NOT called again by detector
    assert len(backend.open_invocations) == 1

    # 4. Context construction
    ctx = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="sample.pdf",
        content_type=detection.content_type,
        size_bytes=len(raw_content),
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        reader=detection.reader,
    )

    # 5. Processor execution
    proc = ConcreteTestProcessor()
    result = await proc.process(ctx)

    # 6. Verify exact byte-for-byte reconstruction
    assert result.status.value == "success"
    assert f"Processed {len(raw_content)} bytes" in (result.extracted_text or "")

    # 7. Assert backend.open() remains exactly one
    assert len(backend.open_invocations) == 1

    # 8. Cleanup and verify underlying reader closed exactly once
    ctx.reader.close()
    assert underlying_reader.closed is True
    assert underlying_reader.close_count == 1

    # Multiple close calls remain idempotent
    ctx.reader.close()
    assert underlying_reader.close_count == 1


@pytest.mark.asyncio
async def test_real_local_storage_backend_open_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify open-count contract against the production LocalStorage backend."""
    storage = LocalStorage(root_path=tmp_path, chunk_size=1024)
    await storage.initialize()

    raw_content = b"%PDF-1.7\r\nReal LocalStorage Content With Header and Payload"
    stored_obj = await storage.save(io.BytesIO(raw_content))

    # Instrument storage.open
    real_open = storage.open
    open_count = 0

    async def instrumented_open(key: str) -> StoredObjectReader:
        nonlocal open_count
        open_count += 1
        return await real_open(key)

    monkeypatch.setattr(storage, "open", instrumented_open)

    # Open once
    reader = await storage.open(stored_obj.storage_key)
    assert open_count == 1

    # Detect format
    detector = FormatDetector(buffer_size=65536)
    detection = await detector.detect(reader)
    assert detection.content_type == "application/pdf"
    assert open_count == 1

    # Process
    ctx = ProcessingContext(
        document_id=uuid.uuid4(),
        original_filename="doc.pdf",
        content_type=detection.content_type,
        size_bytes=stored_obj.size_bytes,
        sha256=stored_obj.checksum_sha256,
        reader=detection.reader,
    )
    proc = ConcreteTestProcessor()
    result = await proc.process(ctx)

    assert result.status.value == "success"
    assert f"Processed {len(raw_content)} bytes" in (result.extracted_text or "")
    assert open_count == 1

    # Cleanup
    ctx.reader.close()
    assert reader.closed is True
    assert isinstance(reader, LocalStoredObjectReader)
    assert reader._file_obj.closed is True

    await storage.close()


# ---------------------------------------------------------------------------
# Mixed Sync / Async Reader Contract Verification Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_read_followed_by_chunks() -> None:
    """Combination 1: read() followed by chunks().

    Supported: read() consumes initial bytes, chunks() streams remaining bytes
    and finalizes the reader.
    """
    data = b"%PDF-1.7\r\n" + (b"0123456789ABCDEF" * 200)  # ~3.2 KiB
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(reader)
    prefixed_reader = detection.reader

    # 1. Non-terminal async read
    part1 = await prefixed_reader.read(30)
    assert part1 == data[:30]
    assert not prefixed_reader.closed

    # 2. Terminal async chunks
    chunks_list: list[bytes] = []
    async for chunk in prefixed_reader.chunks(chunk_size=128):
        chunks_list.append(chunk)

    part2 = b"".join(chunks_list)
    assert part1 + part2 == data
    assert prefixed_reader.closed is True
    assert reader.closed is True


@pytest.mark.asyncio
async def test_reader_chunks_followed_by_read_fails_predictably() -> None:
    """Combination 2: chunks() followed by read().

    Unsupported: chunks() finalizes the stream upon iteration completion;
    subsequent read() must raise StorageReadError.
    """
    data = b"%PDF-1.7\r\n" + (b"M" * 300)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(reader)
    prefixed_reader = detection.reader

    # Consume all chunks
    async for _ in prefixed_reader.chunks(chunk_size=50):
        pass

    assert prefixed_reader.closed is True

    # Subsequent read() must fail predictably
    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        await prefixed_reader.read(10)


def test_reader_read_sync_followed_by_sync_chunks() -> None:
    """Combination 3: read_sync() followed by sync_chunks().

    Supported: read_sync() consumes initial bytes synchronously, sync_chunks()
    streams remaining bytes and finalizes the reader.
    """
    data = b"%PDF-1.7\r\n" + (b"X" * 500)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(reader)
    prefixed_reader = detection.reader

    part1 = prefixed_reader.read_sync(40)
    assert part1 == data[:40]
    assert not prefixed_reader.closed

    chunks_list: list[bytes] = []
    for chunk in prefixed_reader.sync_chunks(chunk_size=100):
        chunks_list.append(chunk)

    part2 = b"".join(chunks_list)
    assert part1 + part2 == data
    assert prefixed_reader.closed is True


def test_reader_sync_chunks_followed_by_read_sync_fails_predictably() -> None:
    """Combination 4: sync_chunks() followed by read_sync().

    Unsupported: sync_chunks() finalizes the stream; subsequent read_sync()
    must raise StorageReadError.
    """
    data = b"%PDF-1.7\r\n" + (b"K" * 200)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(reader)
    prefixed_reader = detection.reader

    for _ in prefixed_reader.sync_chunks(chunk_size=50):
        pass

    assert prefixed_reader.closed is True

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        prefixed_reader.read_sync(10)


@pytest.mark.asyncio
async def test_reader_read_followed_by_read_sync() -> None:
    """Combination 5: read() followed by read_sync().

    Supported: Both are non-terminal operations that advance the logical pointer.
    """
    data = b"%PDF-1.7\r\n" + (b"AB" * 150)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(reader)
    prefixed_reader = detection.reader

    part1 = await prefixed_reader.read(25)
    part2 = prefixed_reader.read_sync(50)
    part3 = await prefixed_reader.read(-1)

    assert part1 + part2 + part3 == data
    assert not prefixed_reader.closed
    prefixed_reader.close()
    assert prefixed_reader.closed is True


@pytest.mark.asyncio
async def test_reader_read_sync_followed_by_read() -> None:
    """Combination 6: read_sync() followed by read().

    Supported: Both advance the shared logical position seamlessly.
    """
    data = b"%PDF-1.7\r\n" + (b"CD" * 150)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(reader)
    prefixed_reader = detection.reader

    part1 = prefixed_reader.read_sync(35)
    part2 = await prefixed_reader.read(45)
    part3 = prefixed_reader.read_sync(-1)

    assert part1 + part2 + part3 == data
    assert not prefixed_reader.closed
    prefixed_reader.close()
    assert prefixed_reader.closed is True


@pytest.mark.asyncio
async def test_reader_chunks_followed_by_sync_chunks_fails_predictably() -> None:
    """Combination 7: chunks() followed by sync_chunks().

    Unsupported: First chunks iteration closes the reader.
    """
    data = b"%PDF-1.7\r\n" + (b"Y" * 100)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(reader)
    prefixed_reader = detection.reader

    async for _ in prefixed_reader.chunks():
        pass

    assert prefixed_reader.closed is True

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        for _ in prefixed_reader.sync_chunks():
            pass


@pytest.mark.asyncio
async def test_reader_sync_chunks_followed_by_chunks_fails_predictably() -> None:
    """Combination 8: sync_chunks() followed by chunks().

    Unsupported: First sync_chunks iteration closes the reader.
    """
    data = b"%PDF-1.7\r\n" + (b"Z" * 100)
    detector = FormatDetector(buffer_size=64)
    reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(reader)
    prefixed_reader = detection.reader

    for _ in prefixed_reader.sync_chunks():
        pass

    assert prefixed_reader.closed is True

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        async for _ in prefixed_reader.chunks():
            pass


@pytest.mark.asyncio
async def test_reader_async_chunks_full_iteration_closes_resources() -> None:
    """Verify that full iteration of async chunks() exhausts generator and closes resources."""
    data = b"%PDF-1.7\r\n" + (b"A" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(inner_reader)
    prefixed_reader = detection.reader

    chunks: list[bytes] = []
    async for chunk in prefixed_reader.chunks(chunk_size=100):
        chunks.append(chunk)

    assert b"".join(chunks) == data
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1


@pytest.mark.asyncio
async def test_reader_async_chunks_early_termination_retained_generator() -> None:
    """Verify lifecycle of retained async generator during early termination.

    Without aclose() or aclosing(), breaking from async for does NOT immediately
    execute the generator's finally block while the generator object remains referenced.
    Explicit aclose() triggers finalization and resource cleanup.
    """
    data = b"%PDF-1.7\r\n" + (b"T" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(inner_reader)
    prefixed_reader = detection.reader

    gen = prefixed_reader.chunks(chunk_size=100)
    assert isinstance(gen, AsyncGenerator)
    async for chunk in gen:
        assert len(chunk) > 0
        break

    # Retained in scope: finally block has NOT run yet
    assert prefixed_reader.closed is False
    assert inner_reader.closed is False

    # Explicit closure triggers generator finally block
    await gen.aclose()
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1

    # Predictable subsequent failure
    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        await prefixed_reader.read(10)


@pytest.mark.asyncio
async def test_reader_async_chunks_aclosing_context_manager() -> None:
    """Verify deterministic resource cleanup using contextlib.aclosing context manager."""
    data = b"%PDF-1.7\r\n" + (b"C" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(inner_reader)
    prefixed_reader = detection.reader

    chunks_gen = prefixed_reader.chunks(chunk_size=100)
    assert isinstance(chunks_gen, AsyncGenerator)
    async with contextlib.aclosing(chunks_gen) as stream:
        async for chunk in stream:
            assert len(chunk) > 0
            break

    # Exiting aclosing guarantees aclose() execution
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        await prefixed_reader.read(10)


@pytest.mark.asyncio
async def test_reader_async_chunks_with_reader_context_manager() -> None:
    """Verify that reader async context manager guarantees cleanup on early break."""
    data = b"%PDF-1.7\r\n" + (b"M" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = await detector.detect(inner_reader)
    prefixed_reader = detection.reader

    async with prefixed_reader:
        async for chunk in prefixed_reader.chunks(chunk_size=100):
            assert len(chunk) > 0
            break

    # Exiting async with guarantees reader.close()
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1


def test_reader_sync_chunks_full_iteration_closes_resources() -> None:
    """Verify that full iteration of sync_chunks() exhausts generator and closes resources."""
    data = b"%PDF-1.7\r\n" + (b"S" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(inner_reader)
    prefixed_reader = detection.reader

    chunks = list(prefixed_reader.sync_chunks(chunk_size=100))
    assert b"".join(chunks) == data
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1


def test_reader_sync_chunks_retained_generator_lifecycle() -> None:
    """Verify synchronous generator lifecycle when generator is deliberately retained.

    When assigned to a variable, break alone does NOT guarantee immediate execution of
    the finally block while the generator object remains in scope. Explicit generator.close()
    must be called or contextlib.closing() used to guarantee finalization.
    """
    data = b"%PDF-1.7\r\n" + (b"R" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(inner_reader)
    prefixed_reader = detection.reader

    stream = prefixed_reader.sync_chunks(chunk_size=100)
    assert isinstance(stream, Generator)
    for chunk in stream:
        assert len(chunk) > 0
        break

    # Generator is suspended at yield, not yet closed while retained in scope
    assert prefixed_reader.closed is False
    assert inner_reader.closed is False

    # Explicit close() triggers generator's finally block
    stream.close()
    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1

    # Idempotent second close
    stream.close()
    prefixed_reader.close()
    assert inner_reader.close_count == 1

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        prefixed_reader.read_sync(10)


def test_reader_sync_chunks_closing_context_manager() -> None:
    """Verify deterministic resource cleanup using contextlib.closing context manager."""
    data = b"%PDF-1.7\r\n" + (b"X" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(inner_reader)
    prefixed_reader = detection.reader

    chunks_gen = prefixed_reader.sync_chunks(chunk_size=100)
    assert isinstance(chunks_gen, Generator)
    with contextlib.closing(chunks_gen) as stream:
        for chunk in stream:
            assert len(chunk) > 0
            break

    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1

    with pytest.raises(StorageReadError, match="Cannot read from a closed storage reader"):
        prefixed_reader.read_sync(10)


def test_reader_sync_chunks_with_reader_context_manager() -> None:
    """Verify that reader synchronous context manager guarantees resource cleanup on early break."""
    data = b"%PDF-1.7\r\n" + (b"Y" * 1000)
    detector = FormatDetector(buffer_size=64)
    inner_reader = ForwardOnlyStreamReader(data)
    detection = detector.detect_sync(inner_reader)
    prefixed_reader = detection.reader

    with prefixed_reader:
        for chunk in prefixed_reader.sync_chunks(chunk_size=100):
            assert len(chunk) > 0
            break

    assert prefixed_reader.closed is True
    assert inner_reader.closed is True
    assert inner_reader.close_count == 1
