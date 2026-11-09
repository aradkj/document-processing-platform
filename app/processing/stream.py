"""Stream adapters for processing forward-only stored object streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from app.storage.base import StoredObjectReader
from app.storage.exceptions import StorageReadError
from app.storage.types import StorageMetadata


class PrefixedStreamReader(StoredObjectReader):
    """Adapter prepending a buffered prefix to a forward-only StoredObjectReader.

    Allows format detectors or inspection utilities to read the initial bytes
    of a stream while guaranteeing that downstream processors can replay and
    read the entire stream from byte zero without reopening the underlying object.

    Memory bounded: Retains only the inspected prefix (e.g. 64 KiB), never buffering
    the entire document in memory.
    """

    def __init__(self, prefix: bytes, inner: StoredObjectReader) -> None:
        self._prefix = prefix
        self._prefix_pos = 0
        self._inner = inner
        self._closed = False

    @property
    def storage_key(self) -> str:
        """The opaque storage key identifying this object."""
        return self._inner.storage_key

    @property
    def metadata(self) -> StorageMetadata:
        """The metadata associated with this stored object."""
        return self._inner.metadata

    @property
    def closed(self) -> bool:
        """Whether the reader is closed."""
        return self._closed or self._inner.closed

    async def read(self, size: int = -1) -> bytes:
        """Asynchronously read up to size bytes, consuming the prefix first."""
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")

        if size == 0:
            return b""

        # Case 1: Read all remaining bytes (size < 0)
        if size < 0:
            prefix_rem = self._prefix[self._prefix_pos :]
            self._prefix_pos = len(self._prefix)
            inner_rem = await self._inner.read(-1)
            return prefix_rem + inner_rem

        # Case 2: Read bounded size (size > 0)
        prefix_avail = len(self._prefix) - self._prefix_pos
        if prefix_avail > 0:
            if size <= prefix_avail:
                chunk = self._prefix[self._prefix_pos : self._prefix_pos + size]
                self._prefix_pos += size
                return chunk
            chunk = self._prefix[self._prefix_pos :]
            self._prefix_pos = len(self._prefix)
            needed = size - len(chunk)
            inner_chunk = await self._inner.read(needed)
            return chunk + inner_chunk

        return await self._inner.read(size)

    def read_sync(self, size: int = -1) -> bytes:
        """Synchronously read up to size bytes, consuming the prefix first."""
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")

        if size == 0:
            return b""

        # Case 1: Read all remaining bytes (size < 0)
        if size < 0:
            prefix_rem = self._prefix[self._prefix_pos :]
            self._prefix_pos = len(self._prefix)
            inner_rem = self._inner.read_sync(-1)
            return prefix_rem + inner_rem

        # Case 2: Read bounded size (size > 0)
        prefix_avail = len(self._prefix) - self._prefix_pos
        if prefix_avail > 0:
            if size <= prefix_avail:
                chunk = self._prefix[self._prefix_pos : self._prefix_pos + size]
                self._prefix_pos += size
                return chunk
            chunk = self._prefix[self._prefix_pos :]
            self._prefix_pos = len(self._prefix)
            needed = size - len(chunk)
            inner_chunk = self._inner.read_sync(needed)
            return chunk + inner_chunk

        return self._inner.read_sync(size)

    async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Asynchronously stream chunks of bytes from the prefixed stream."""
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")

        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        try:
            while self._prefix_pos < len(self._prefix):
                avail = len(self._prefix) - self._prefix_pos
                take = min(avail, step)
                chunk = self._prefix[self._prefix_pos : self._prefix_pos + take]
                self._prefix_pos += take
                yield chunk

            async for inner_chunk in self._inner.chunks(chunk_size):
                yield inner_chunk
        finally:
            self.close()

    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        """Synchronously stream chunks of bytes from the prefixed stream."""
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")

        step = chunk_size if (chunk_size is not None and chunk_size > 0) else 65536
        try:
            while self._prefix_pos < len(self._prefix):
                avail = len(self._prefix) - self._prefix_pos
                take = min(avail, step)
                chunk = self._prefix[self._prefix_pos : self._prefix_pos + take]
                self._prefix_pos += take
                yield chunk

            yield from self._inner.sync_chunks(chunk_size)
        finally:
            self.close()

    def close(self) -> None:
        """Close the adapter and propagate closure to the underlying reader."""
        if not self._closed:
            self._closed = True
            self._inner.close()
