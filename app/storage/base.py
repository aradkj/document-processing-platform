"""Abstract storage backend and reader interfaces."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator
from typing import Any, BinaryIO

from app.storage.types import BinaryStream, StorageMetadata, StoredObject


class StoredObjectReader(ABC):
    """Abstract interface for reading a stored object as a stream."""

    @property
    @abstractmethod
    def storage_key(self) -> str:
        """The opaque storage key identifying this object."""
        ...

    @property
    @abstractmethod
    def metadata(self) -> StorageMetadata:
        """The metadata associated with this stored object."""
        ...

    @property
    @abstractmethod
    def closed(self) -> bool:
        """Whether the reader is closed."""
        ...

    @abstractmethod
    def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Asynchronously stream chunks of bytes from the stored object."""
        ...

    @abstractmethod
    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        """Synchronously stream chunks of bytes from the stored object."""
        ...

    @abstractmethod
    async def read(self, size: int = -1) -> bytes:
        """Asynchronously read up to `size` bytes from the object.

        Warning:
            If `size` is negative or omitted, all remaining content is read into memory at once.
            For large files and bounded-memory streaming, use `chunks()` instead.
        """
        ...

    @abstractmethod
    def read_sync(self, size: int = -1) -> bytes:
        """Synchronously read up to `size` bytes from the object.

        Warning:
            If `size` is negative or omitted, all remaining content is read into memory at once.
            For large files and bounded-memory streaming, use `sync_chunks()` instead.
        """
        ...

    @abstractmethod
    def close(self) -> None:
        """Close the underlying file handle and release resources."""
        ...

    async def __aenter__(self) -> "StoredObjectReader":
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit with guaranteed resource cleanup."""
        self.close()

    def __enter__(self) -> "StoredObjectReader":
        """Synchronous context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Synchronous context manager exit with guaranteed resource cleanup."""
        self.close()


class StorageBackend(ABC):
    """Abstract base class defining neutral object storage operations.

    Designed to support multiple backends (Local filesystem, S3/MinIO) without
    coupling to web framework, ORM models, or document business rules.
    """

    @abstractmethod
    async def save(
        self,
        stream: BinaryIO | BinaryStream | AsyncIterator[bytes] | Iterator[bytes],
        *,
        storage_key: str | None = None,
    ) -> StoredObject:
        """Stream data from input and persist to storage.

        Args:
            stream: Binary stream, async iterator, or sync iterator yielding bytes.
            storage_key: Optional explicit storage key. If omitted, a secure key is generated.

        Returns:
            StoredObject: Persistence confirmation containing key, size, and SHA-256 hash.
        """
        ...

    @abstractmethod
    async def open(self, storage_key: str) -> StoredObjectReader:
        """Open a stored object for streaming reading.

        Args:
            storage_key: Opaque key identifying the object.

        Returns:
            StoredObjectReader: Streaming reader instance.
        """
        ...

    @abstractmethod
    async def delete(self, storage_key: str, *, missing_ok: bool = False) -> bool:
        """Delete an object from storage.

        Args:
            storage_key: Opaque key identifying the object.
            missing_ok: If False, raises StorageObjectNotFoundError when object does not exist.
                        If True, returns False safely when object is missing.

        Returns:
            bool: True if object existed and was deleted, False if missing and missing_ok=True.
        """
        ...

    @abstractmethod
    async def exists(self, storage_key: str) -> bool:
        """Check whether an object exists in storage.

        Args:
            storage_key: Opaque key identifying the object.

        Returns:
            bool: True if object exists and is a valid stored file, False otherwise.
        """
        ...

    @abstractmethod
    async def get_metadata(self, storage_key: str) -> StorageMetadata:
        """Retrieve metadata for a stored object without reading its binary content.

        Args:
            storage_key: Opaque key identifying the object.

        Returns:
            StorageMetadata: Metadata including key, size, timestamps, and checksum if available.
        """
        ...

    @abstractmethod
    def generate_key(self) -> str:
        """Generate a cryptographically secure, opaque storage key."""
        ...

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize backend resources, directories, or connection pools at startup."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up and close any open connections or background resources on shutdown."""
        ...
