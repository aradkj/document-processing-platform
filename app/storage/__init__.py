"""Storage package initialization and public interface."""

from functools import lru_cache
from typing import Any, cast

from fastapi import Request

from app.core.config import Settings, get_settings
from app.storage.base import StorageBackend, StoredObjectReader
from app.storage.exceptions import (
    StorageConfigurationError,
    StorageDeleteError,
    StorageError,
    StorageInvalidKeyError,
    StorageObjectAlreadyExistsError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageReadError,
    StorageSecurityError,
    StorageUnsupportedFilesystemError,
    StorageWriteError,
)
from app.storage.local import LocalStorage, normalize_and_validate_key
from app.storage.types import BinaryStream, StorageMetadata, StoredObject


def create_storage_backend(settings: Settings | None = None) -> StorageBackend:
    """Instantiate a storage backend based on application settings."""
    cfg = settings or get_settings()
    if cfg.STORAGE_BACKEND == "local":
        return LocalStorage(
            root_path=cfg.STORAGE_LOCAL_ROOT,
            chunk_size=cfg.STORAGE_CHUNK_SIZE,
        )
    raise StorageConfigurationError(f"Unsupported storage backend: '{cfg.STORAGE_BACKEND}'")


@lru_cache
def get_default_storage() -> StorageBackend:
    """Return a cached default storage backend instance."""
    return create_storage_backend()


def clear_storage_cache() -> None:
    """Clear cached default storage backend instance (useful during testing)."""
    get_default_storage.cache_clear()


_DEFAULT_REQUEST: Any = None


def get_storage(request: Request = _DEFAULT_REQUEST) -> StorageBackend:
    """Resolve active storage backend from FastAPI application state or fallback to default."""
    if request is not None and hasattr(request.app.state, "storage"):
        return cast(StorageBackend, request.app.state.storage)
    return get_default_storage()


__all__ = [
    "BinaryStream",
    "LocalStorage",
    "StorageBackend",
    "StorageConfigurationError",
    "StorageDeleteError",
    "StorageError",
    "StorageInvalidKeyError",
    "StorageMetadata",
    "StorageObjectAlreadyExistsError",
    "StorageObjectNotFoundError",
    "StoragePermissionError",
    "StorageReadError",
    "StorageSecurityError",
    "StorageUnsupportedFilesystemError",
    "StorageWriteError",
    "StoredObject",
    "StoredObjectReader",
    "clear_storage_cache",
    "create_storage_backend",
    "get_default_storage",
    "get_storage",
    "normalize_and_validate_key",
]
