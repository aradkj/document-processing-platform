"""Local filesystem storage backend implementation."""

import asyncio
import contextlib
import errno
import hashlib
import logging
import os
import re
import secrets
import stat
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

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
from app.storage.types import BinaryStream, StorageMetadata, StoredObject

logger = logging.getLogger(__name__)

# Strict format for partitioned storage keys: 2 hex chars / 2 hex chars / 32 hex chars
# Example: 4f/8d/4f8d9b2e1c3a4e5b6c7d8e9f0a1b2c3d
PARTITIONED_KEY_REGEX = re.compile(r"^[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{32}$")
FLAT_HEX_KEY_REGEX = re.compile(r"^[a-f0-9]{32}$")


def normalize_and_validate_key(key: str) -> str:
    """Validate and normalize a storage key to strict partitioned format.

    Rejects:
    - Empty or whitespace-only keys
    - Path traversal sequences (e.g. '..', '.')
    - Backslashes, null bytes, special characters, uppercase hex
    - Absolute or outside-root paths

    Accepts:
    - Partitioned format: 'ab/cd/abcdef0123456789abcdef0123456789'
    - Flat 32-hex format: 'abcdef0123456789abcdef0123456789' (normalized to 'ab/cd/abcdef...')
    """
    if not isinstance(key, str) or not key.strip():
        raise StorageInvalidKeyError("Storage key must be a non-empty string.")

    cleaned = key.strip()

    # Reject null bytes and path traversal patterns immediately
    if "\0" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise StorageInvalidKeyError("Storage key contains invalid characters or path traversal.")

    if FLAT_HEX_KEY_REGEX.match(cleaned):
        return f"{cleaned[:2]}/{cleaned[2:4]}/{cleaned}"

    if PARTITIONED_KEY_REGEX.match(cleaned):
        return cleaned

    raise StorageInvalidKeyError("Storage key does not match required opaque hex format.")


# Detect if descriptor-relative directory operations (dir_fd) are supported by OS kernel
_SUPPORTS_DIR_FD: bool = bool(
    os.supports_dir_fd
    and os.open in os.supports_dir_fd
    and os.link in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


@contextlib.contextmanager
def _open_partition_dir_fds(
    root_path: Path, part1: str, part2: str, *, create: bool = False
) -> Iterator[tuple[int, int, int]]:
    """Open directory descriptors for partitions using O_DIRECTORY | O_NOFOLLOW.

    This pins directory descriptors to prevent TOCTOU symlink substitution
    between path resolution and subsequent file operations on POSIX platforms.
    """
    if create:
        try:
            root_path.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise StoragePermissionError("Permission denied creating storage root.") from exc
        except OSError as exc:
            raise StorageWriteError("Failed to create storage root directory.") from exc

    root_fd = -1
    part1_fd = -1
    part2_fd = -1
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags_sub = flags_dir | getattr(os, "O_NOFOLLOW", 0)

    try:
        try:
            root_fd = os.open(str(root_path), flags_dir)
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError("Storage root does not exist.") from exc
        except PermissionError as exc:
            raise StoragePermissionError("Permission denied accessing storage root.") from exc
        except OSError as exc:
            raise StorageReadError("Failed to open storage root directory.") from exc

        if create:
            try:
                os.mkdir(part1, dir_fd=root_fd)
            except FileExistsError:
                pass
            except PermissionError as exc:
                raise StoragePermissionError(
                    "Permission denied creating partition directory."
                ) from exc
            except OSError as exc:
                raise StorageWriteError("Failed to create partition directory.") from exc

        try:
            part1_fd = os.open(part1, flags_sub, dir_fd=root_fd)
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError("Storage partition not found.") from exc
        except PermissionError as exc:
            raise StoragePermissionError(
                "Permission denied accessing partition directory."
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageSecurityError(
                    "Intermediate partition directory is a symlink or invalid."
                ) from exc
            raise StorageReadError("Failed to open partition directory.") from exc

        if create:
            try:
                os.mkdir(part2, dir_fd=part1_fd)
            except FileExistsError:
                pass
            except PermissionError as exc:
                raise StoragePermissionError(
                    "Permission denied creating partition directory."
                ) from exc
            except OSError as exc:
                raise StorageWriteError("Failed to create partition directory.") from exc

        try:
            part2_fd = os.open(part2, flags_sub, dir_fd=part1_fd)
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError("Storage partition not found.") from exc
        except PermissionError as exc:
            raise StoragePermissionError(
                "Permission denied accessing partition directory."
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageSecurityError(
                    "Intermediate partition directory is a symlink or invalid."
                ) from exc
            raise StorageReadError("Failed to open partition directory.") from exc

        yield root_fd, part1_fd, part2_fd
    finally:
        for fd in (part2_fd, part1_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


class LocalStoredObjectReader(StoredObjectReader):
    """Streaming reader for objects stored on the local filesystem."""

    def __init__(
        self,
        storage_key: str,
        file_obj: BinaryIO,
        metadata: StorageMetadata,
        default_chunk_size: int,
    ) -> None:
        self._storage_key = storage_key
        self._file_obj = file_obj
        self._metadata = metadata
        self._default_chunk_size = default_chunk_size
        self._closed = False

    @property
    def storage_key(self) -> str:
        return self._storage_key

    @property
    def metadata(self) -> StorageMetadata:
        return self._metadata

    @property
    def closed(self) -> bool:
        return self._closed

    async def read(self, size: int = -1) -> bytes:
        """Asynchronously read up to `size` bytes from the file.

        Warning:
            If `size` is negative or omitted, all remaining content is read into memory.
            For large files, use `chunks()` for bounded-memory streaming.
        """
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        try:
            return await asyncio.to_thread(self._file_obj.read, size)
        except Exception as exc:
            raise StorageReadError("Failed to read from stored object.") from exc

    def read_sync(self, size: int = -1) -> bytes:
        """Synchronously read up to `size` bytes from the file.

        Warning:
            If `size` is negative or omitted, all remaining content is read into memory.
            For large files, use `sync_chunks()` for bounded-memory streaming.
        """
        if self._closed:
            raise StorageReadError("Cannot read from a closed storage reader.")
        try:
            return self._file_obj.read(size)
        except Exception as exc:
            raise StorageReadError("Failed to read from stored object.") from exc

    async def chunks(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        """Asynchronously stream chunks of bytes off the event loop."""
        size = chunk_size or self._default_chunk_size
        try:
            while not self._closed:
                chunk = await asyncio.to_thread(self._file_obj.read, size)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:
            raise StorageReadError("Error streaming chunks from stored object.") from exc
        finally:
            self.close()

    def sync_chunks(self, chunk_size: int | None = None) -> Iterator[bytes]:
        """Synchronously stream chunks of bytes."""
        size = chunk_size or self._default_chunk_size
        try:
            while not self._closed:
                chunk = self._file_obj.read(size)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:
            raise StorageReadError("Error streaming chunks from stored object.") from exc
        finally:
            self.close()

    def close(self) -> None:
        """Close underlying file handle safely."""
        if not self._closed:
            self._closed = True
            try:
                self._file_obj.close()
            except Exception:
                pass


class LocalStorage(StorageBackend):
    """Local filesystem storage backend.

    Stores objects in a partitioned directory hierarchy based on cryptographically
    opaque keys, protecting against path traversal, symlink escapes, and directory
    contention.
    """

    def __init__(
        self,
        root_path: str | Path,
        chunk_size: int = 1048576,
    ) -> None:
        self.root_path = Path(root_path).resolve()
        self.chunk_size = chunk_size

    def generate_key(self) -> str:
        """Generate a cryptographically secure 32-hex random storage key."""
        return secrets.token_hex(16)

    @staticmethod
    def is_valid_key(storage_key: str) -> bool:
        """Check whether a given storage key adheres to the valid opaque format."""
        if not isinstance(storage_key, str) or not storage_key.strip():
            return False
        cleaned = storage_key.strip()
        if "\0" in cleaned or "\\" in cleaned or ".." in cleaned:
            return False
        return bool(FLAT_HEX_KEY_REGEX.match(cleaned) or PARTITIONED_KEY_REGEX.match(cleaned))

    def resolve_path(self, storage_key: str) -> Path:
        """Validate key and resolve canonical filesystem path under storage root."""
        return self._resolve_path(storage_key)

    def _resolve_path(self, storage_key: str) -> Path:
        """Validate key and resolve canonical filesystem path under storage root."""
        normalized_key = normalize_and_validate_key(storage_key)
        target = self.root_path / normalized_key

        # Check for symlink in target or intermediate path segments
        curr = target
        while curr != self.root_path and curr != self.root_path.parent:
            if curr.is_symlink():
                raise StorageSecurityError("Symlinked storage paths are strictly forbidden.")
            curr = curr.parent

        try:
            resolved_target = target.resolve()
            resolved_root = self.root_path.resolve()
        except Exception as exc:
            raise StorageInvalidKeyError("Unable to resolve storage path.") from exc

        # Strict containment verification
        if not resolved_target.is_relative_to(resolved_root):
            raise StorageSecurityError("Storage key path traversal or escape detected.")

        return resolved_target

    def _cleanup_empty_parents(self, directory: Path) -> None:
        """Recursively remove empty parent directories up to storage root."""
        resolved_root = self.root_path.resolve()
        curr = directory
        while curr != self.root_path and curr != self.root_path.parent:
            if curr.is_symlink():
                break
            try:
                resolved_curr = curr.resolve()
                if not resolved_curr.is_relative_to(resolved_root):
                    break
            except Exception:
                break
            try:
                # rmdir only removes directory if completely empty
                curr.rmdir()
                curr = curr.parent
            except OSError:
                break

    async def initialize(self) -> None:
        """Ensure storage directory exists and verify write permissions."""
        try:
            self.root_path.mkdir(parents=True, exist_ok=True)
            probe = self.root_path / f".probe_{secrets.token_hex(4)}"
            probe.touch()
            probe.unlink()
        except PermissionError as exc:
            logger.error("Storage root directory is not writable.")
            raise StoragePermissionError("Storage root directory is not writable.") from exc
        except Exception as exc:
            logger.error("Failed to initialize storage root: %s", exc)
            raise StorageConfigurationError("Failed to initialize storage root directory.") from exc

    async def close(self) -> None:
        """Clean up local storage resources."""
        pass

    async def save(
        self,
        stream: BinaryIO | BinaryStream | AsyncIterator[bytes] | Iterator[bytes],
        *,
        storage_key: str | None = None,
    ) -> StoredObject:
        """Persist data from input stream to local filesystem using safe atomic publication."""
        if storage_key is not None:
            raw_key = storage_key
        else:
            raw_key = self.generate_key()

        key = normalize_and_validate_key(raw_key)
        parts = key.split("/")
        part1, part2, filename = parts[0], parts[1], parts[2]

        if _SUPPORTS_DIR_FD:
            with _open_partition_dir_fds(self.root_path, part1, part2, create=True) as (
                _,
                _,
                p2_fd,
            ):
                # Pre-check if target object already exists
                try:
                    os.stat(filename, dir_fd=p2_fd, follow_symlinks=False)
                    raise StorageObjectAlreadyExistsError("Storage object already exists.")
                except FileNotFoundError:
                    pass

                # Open staging file directly within pinned directory descriptor
                temp_name = f".tmp_{secrets.token_hex(8)}_{filename}"
                create_flags = (
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    temp_fd = os.open(temp_name, create_flags, 0o600, dir_fd=p2_fd)
                except PermissionError as exc:
                    raise StoragePermissionError(
                        "Permission denied creating temporary staging file."
                    ) from exc
                except OSError as exc:
                    raise StorageWriteError("Failed to create temporary staging file.") from exc

                hasher = hashlib.sha256()
                bytes_written = 0
                temp_closed = False

                try:
                    if hasattr(stream, "read"):

                        def _write_sync_stream() -> tuple[int, str]:
                            h = hashlib.sha256()
                            count = 0
                            with open(temp_fd, "wb", closefd=True) as out_f:
                                while True:
                                    buf = stream.read(self.chunk_size)
                                    if not buf:
                                        break
                                    out_f.write(buf)
                                    h.update(buf)
                                    count += len(buf)
                            return count, h.hexdigest()

                        temp_closed = True
                        bytes_written, sha256_digest = await asyncio.to_thread(_write_sync_stream)
                    elif hasattr(stream, "__aiter__"):
                        temp_closed = True
                        with open(temp_fd, "wb", closefd=True) as out_f:
                            async for chunk in stream:
                                if chunk:
                                    out_f.write(chunk)
                                    hasher.update(chunk)
                                    bytes_written += len(chunk)
                        sha256_digest = hasher.hexdigest()
                    elif hasattr(stream, "__iter__"):

                        def _write_sync_iter() -> tuple[int, str]:
                            h = hashlib.sha256()
                            count = 0
                            with open(temp_fd, "wb", closefd=True) as out_f:
                                for chunk in stream:
                                    if chunk:
                                        out_f.write(chunk)
                                        h.update(chunk)
                                        count += len(chunk)
                            return count, h.hexdigest()

                        temp_closed = True
                        bytes_written, sha256_digest = await asyncio.to_thread(_write_sync_iter)
                    else:
                        raise StorageWriteError("Unsupported input stream type.")

                    # Atomically publish temporary staging file to target path
                    try:
                        os.link(
                            temp_name,
                            filename,
                            src_dir_fd=p2_fd,
                            dst_dir_fd=p2_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise StorageObjectAlreadyExistsError(
                            "Storage object already exists."
                        ) from exc
                    except OSError as exc:
                        if exc.errno == errno.EEXIST:
                            raise StorageObjectAlreadyExistsError(
                                "Storage object already exists."
                            ) from exc
                        if exc.errno in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
                            raise StorageUnsupportedFilesystemError(
                                f"Filesystem lacks atomic hard-link support (errno={exc.errno})."
                            ) from exc
                        raise StorageWriteError(
                            "Failed to persist storage object to disk."
                        ) from exc
                except StorageError:
                    raise
                except PermissionError as exc:
                    raise StoragePermissionError(
                        "Permission denied writing storage object."
                    ) from exc
                except OSError as exc:
                    raise StorageWriteError("Failed to persist storage object to disk.") from exc
                finally:
                    if not temp_closed:
                        try:
                            os.close(temp_fd)
                        except OSError:
                            pass
                    try:
                        os.unlink(temp_name, dir_fd=p2_fd)
                    except OSError:
                        pass
        else:
            # Fallback path for platforms without dir_fd support (e.g. Windows)
            target_path = self._resolve_path(key)

            if target_path.exists():
                raise StorageObjectAlreadyExistsError("Storage object already exists.")

            parent_dir = target_path.parent
            try:
                parent_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise StoragePermissionError(
                    "Permission denied creating storage directory."
                ) from exc
            except OSError as exc:
                raise StorageWriteError("Failed to create storage directory.") from exc

            # Verify parent directory is not a symlink after creation
            curr = parent_dir
            while curr != self.root_path and curr != self.root_path.parent:
                if curr.is_symlink():
                    raise StorageSecurityError("Symlinked storage directory detected.")
                curr = curr.parent

            temp_name = f".tmp_{secrets.token_hex(8)}_{target_path.name}"
            temp_path = parent_dir / temp_name

            hasher = hashlib.sha256()
            bytes_written = 0

            try:
                if hasattr(stream, "read"):

                    def _write_sync_stream_path() -> tuple[int, str]:
                        h = hashlib.sha256()
                        count = 0
                        with open(temp_path, "xb") as out_f:
                            while True:
                                buf = stream.read(self.chunk_size)
                                if not buf:
                                    break
                                out_f.write(buf)
                                h.update(buf)
                                count += len(buf)
                        return count, h.hexdigest()

                    bytes_written, sha256_digest = await asyncio.to_thread(_write_sync_stream_path)
                elif hasattr(stream, "__aiter__"):
                    with open(temp_path, "xb") as out_f:
                        async for chunk in stream:
                            if chunk:
                                out_f.write(chunk)
                                hasher.update(chunk)
                                bytes_written += len(chunk)
                    sha256_digest = hasher.hexdigest()
                elif hasattr(stream, "__iter__"):

                    def _write_sync_iter_path() -> tuple[int, str]:
                        h = hashlib.sha256()
                        count = 0
                        with open(temp_path, "xb") as out_f:
                            for chunk in stream:
                                if chunk:
                                    out_f.write(chunk)
                                    h.update(chunk)
                                    count += len(chunk)
                        return count, h.hexdigest()

                    bytes_written, sha256_digest = await asyncio.to_thread(_write_sync_iter_path)
                else:
                    raise StorageWriteError("Unsupported input stream type.")

                # Atomically publish temporary file to target path without allowing overwrite
                try:
                    os.link(temp_path, target_path)
                except FileExistsError as exc:
                    raise StorageObjectAlreadyExistsError("Storage object already exists.") from exc
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise StorageObjectAlreadyExistsError(
                            "Storage object already exists."
                        ) from exc
                    if exc.errno in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV):
                        raise StorageUnsupportedFilesystemError(
                            f"Filesystem lacks atomic hard-link support (errno={exc.errno})."
                        ) from exc
                    raise StorageWriteError("Failed to persist storage object to disk.") from exc
            except StorageError:
                raise
            except PermissionError as exc:
                raise StoragePermissionError("Permission denied writing storage object.") from exc
            except OSError as exc:
                raise StorageWriteError("Failed to persist storage object to disk.") from exc
            finally:
                temp_path.unlink(missing_ok=True)

        return StoredObject(
            storage_key=raw_key,
            size_bytes=bytes_written,
            checksum_sha256=sha256_digest,
            created_at=datetime.now(UTC),
        )

    async def open(self, storage_key: str) -> StoredObjectReader:
        """Open a stored object for streaming reading."""
        key = normalize_and_validate_key(storage_key)
        parts = key.split("/")
        part1, part2, filename = parts[0], parts[1], parts[2]

        if _SUPPORTS_DIR_FD:
            with _open_partition_dir_fds(self.root_path, part1, part2, create=False) as (
                _,
                _,
                p2_fd,
            ):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(filename, flags, dir_fd=p2_fd)
                except FileNotFoundError as exc:
                    raise StorageObjectNotFoundError("Storage object not found.") from exc
                except PermissionError as exc:
                    raise StoragePermissionError(
                        "Permission denied opening storage object."
                    ) from exc
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise StorageSecurityError(
                            "Symlinked storage objects are strictly forbidden."
                        ) from exc
                    raise StorageReadError("Failed to open storage object for reading.") from exc

                try:
                    stat_res = os.fstat(fd)
                    if not stat.S_ISREG(stat_res.st_mode):
                        raise StorageSecurityError("Storage object is not a regular file.")

                    metadata = StorageMetadata(
                        storage_key=storage_key,
                        size_bytes=stat_res.st_size,
                        created_at=datetime.fromtimestamp(stat_res.st_ctime, tz=UTC),
                        modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC),
                    )
                    file_obj = open(fd, "rb", closefd=True)
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise

                return LocalStoredObjectReader(
                    storage_key=storage_key,
                    file_obj=file_obj,
                    metadata=metadata,
                    default_chunk_size=self.chunk_size,
                )
        else:
            target_path = self._resolve_path(key)

            if not target_path.exists() or not target_path.is_file():
                raise StorageObjectNotFoundError("Storage object not found.")

            if target_path.is_symlink():
                raise StorageSecurityError("Symlinked storage objects are strictly forbidden.")

            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(target_path, flags)
            except FileNotFoundError as exc:
                raise StorageObjectNotFoundError("Storage object not found.") from exc
            except PermissionError as exc:
                raise StoragePermissionError("Permission denied opening storage object.") from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP or target_path.is_symlink():
                    raise StorageSecurityError(
                        "Symlinked storage objects are strictly forbidden."
                    ) from exc
                raise StorageReadError("Failed to open storage object for reading.") from exc

            try:
                stat_res = os.fstat(fd)
                if not stat.S_ISREG(stat_res.st_mode):
                    raise StorageSecurityError("Storage object is not a regular file.")

                metadata = StorageMetadata(
                    storage_key=storage_key,
                    size_bytes=stat_res.st_size,
                    created_at=datetime.fromtimestamp(stat_res.st_ctime, tz=UTC),
                    modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC),
                )
                file_obj = open(fd, "rb", closefd=True)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            return LocalStoredObjectReader(
                storage_key=storage_key,
                file_obj=file_obj,
                metadata=metadata,
                default_chunk_size=self.chunk_size,
            )

    async def delete(self, storage_key: str, *, missing_ok: bool = False) -> bool:
        """Delete an object from local filesystem storage."""
        try:
            key = normalize_and_validate_key(storage_key)
        except StorageInvalidKeyError:
            if missing_ok:
                return False
            raise
        parts = key.split("/")
        part1, part2, filename = parts[0], parts[1], parts[2]

        if _SUPPORTS_DIR_FD:
            try:
                with _open_partition_dir_fds(self.root_path, part1, part2, create=False) as (
                    root_fd,
                    p1_fd,
                    p2_fd,
                ):
                    try:
                        st = os.stat(filename, dir_fd=p2_fd, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        if missing_ok:
                            return False
                        raise StorageObjectNotFoundError("Storage object not found.") from exc

                    if stat.S_ISLNK(st.st_mode):
                        raise StorageSecurityError(
                            "Symlinked storage objects are strictly forbidden."
                        )

                    try:
                        os.unlink(filename, dir_fd=p2_fd)
                    except FileNotFoundError as exc:
                        if missing_ok:
                            return False
                        raise StorageObjectNotFoundError("Storage object not found.") from exc
                    except PermissionError as exc:
                        raise StoragePermissionError(
                            "Permission denied deleting storage object."
                        ) from exc
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            raise StorageSecurityError(
                                "Symlinked storage objects are strictly forbidden."
                            ) from exc
                        raise StorageDeleteError("Failed to delete storage object.") from exc

                    # Clean up empty parent partition directories
                    try:
                        os.rmdir(part2, dir_fd=p1_fd)
                        os.rmdir(part1, dir_fd=root_fd)
                    except OSError:
                        pass
                    return True
            except StorageObjectNotFoundError:
                if missing_ok:
                    return False
                raise
        else:
            target_path = self._resolve_path(key)

            if not target_path.exists():
                if missing_ok:
                    return False
                raise StorageObjectNotFoundError("Storage object not found.")

            if target_path.is_symlink():
                raise StorageSecurityError("Symlinked storage objects are strictly forbidden.")

            try:
                target_path.unlink()
                self._cleanup_empty_parents(target_path.parent)
                return True
            except PermissionError as exc:
                raise StoragePermissionError("Permission denied deleting storage object.") from exc
            except FileNotFoundError as exc:
                if missing_ok:
                    return False
                raise StorageObjectNotFoundError("Storage object not found.") from exc
            except OSError as exc:
                raise StorageDeleteError("Failed to delete storage object.") from exc

    async def exists(self, storage_key: str) -> bool:
        """Check whether a storage object exists and is a regular file."""
        try:
            key = normalize_and_validate_key(storage_key)
        except StorageInvalidKeyError:
            return False

        if _SUPPORTS_DIR_FD:
            parts = key.split("/")
            part1, part2, filename = parts[0], parts[1], parts[2]
            try:
                with _open_partition_dir_fds(self.root_path, part1, part2, create=False) as (
                    _,
                    _,
                    p2_fd,
                ):
                    stat_res = os.stat(filename, dir_fd=p2_fd, follow_symlinks=False)
                    return stat.S_ISREG(stat_res.st_mode)
            except (
                StorageObjectNotFoundError,
                StorageSecurityError,
                StorageError,
                FileNotFoundError,
                OSError,
            ):
                return False
        else:
            try:
                target_path = self._resolve_path(key)
                return target_path.is_file() and not target_path.is_symlink()
            except StorageError:
                return False

    async def get_metadata(self, storage_key: str) -> StorageMetadata:
        """Retrieve metadata for a stored object."""
        key = normalize_and_validate_key(storage_key)
        parts = key.split("/")
        part1, part2, filename = parts[0], parts[1], parts[2]

        if _SUPPORTS_DIR_FD:
            with _open_partition_dir_fds(self.root_path, part1, part2, create=False) as (
                _,
                _,
                p2_fd,
            ):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(filename, flags, dir_fd=p2_fd)
                except FileNotFoundError as exc:
                    raise StorageObjectNotFoundError("Storage object not found.") from exc
                except PermissionError as exc:
                    raise StoragePermissionError(
                        "Permission denied reading storage metadata."
                    ) from exc
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise StorageSecurityError(
                            "Symlinked storage objects are strictly forbidden."
                        ) from exc
                    raise StorageReadError("Failed to read storage metadata.") from exc

                try:
                    stat_res = os.fstat(fd)
                    if not stat.S_ISREG(stat_res.st_mode):
                        raise StorageSecurityError("Storage object is not a regular file.")
                    return StorageMetadata(
                        storage_key=storage_key,
                        size_bytes=stat_res.st_size,
                        created_at=datetime.fromtimestamp(stat_res.st_ctime, tz=UTC),
                        modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC),
                    )
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        else:
            target_path = self._resolve_path(key)

            if not target_path.exists() or not target_path.is_file():
                raise StorageObjectNotFoundError("Storage object not found.")

            if target_path.is_symlink():
                raise StorageSecurityError("Symlinked storage objects are strictly forbidden.")

            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(target_path, flags)
            except FileNotFoundError as exc:
                raise StorageObjectNotFoundError("Storage object not found.") from exc
            except PermissionError as exc:
                raise StoragePermissionError("Permission denied reading storage metadata.") from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP or target_path.is_symlink():
                    raise StorageSecurityError(
                        "Symlinked storage objects are strictly forbidden."
                    ) from exc
                raise StorageReadError("Failed to read storage metadata.") from exc

            try:
                stat_res = os.fstat(fd)
                if not stat.S_ISREG(stat_res.st_mode):
                    raise StorageSecurityError("Storage object is not a regular file.")
                return StorageMetadata(
                    storage_key=storage_key,
                    size_bytes=stat_res.st_size,
                    created_at=datetime.fromtimestamp(stat_res.st_ctime, tz=UTC),
                    modified_at=datetime.fromtimestamp(stat_res.st_mtime, tz=UTC),
                )
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
