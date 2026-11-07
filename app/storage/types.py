"""Storage subsystem data structures, protocols, and types."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StorageMetadata:
    """Metadata describing a stored object."""

    storage_key: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    checksum_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Summary representation returned after successfully persisting an object."""

    storage_key: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime


@runtime_checkable
class BinaryStream(Protocol):
    """Protocol for binary stream inputs supporting chunked read operations."""

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes from the stream."""
        ...
