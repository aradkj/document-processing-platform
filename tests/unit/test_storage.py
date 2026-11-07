"""Comprehensive unit tests for the storage foundation and LocalStorage implementation."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from app.storage.exceptions import (
    StorageConfigurationError,
    StorageInvalidKeyError,
    StorageObjectAlreadyExistsError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageReadError,
    StorageSecurityError,
    StorageUnsupportedFilesystemError,
    StorageWriteError,
)
from app.storage.local import _SUPPORTS_DIR_FD, LocalStorage


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary storage directory."""
    store_dir = tmp_path / "filestore"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


@pytest.fixture
async def storage(storage_dir: Path) -> LocalStorage:
    """Fixture providing an initialized LocalStorage instance with a 1024-byte chunk size."""
    backend = LocalStorage(root_path=storage_dir, chunk_size=1024)
    await backend.initialize()
    return backend


# ---------------------------------------------------------------------------
# 1. Key Generation and Validation
# ---------------------------------------------------------------------------


def test_generate_key(storage: LocalStorage) -> None:
    """Verify that generated keys are 32-character lowercase hex strings."""
    key1 = storage.generate_key()
    key2 = storage.generate_key()
    assert len(key1) == 32
    assert len(key2) == 32
    assert key1 != key2
    assert storage.is_valid_key(key1) is True
    assert storage.is_valid_key(key2) is True


@pytest.mark.parametrize(
    "invalid_key",
    [
        "",
        "abc",
        "a" * 31,
        "a" * 33,
        "../etc/passwd",
        "..\\windows\\system32",
        "0123456789abcdef0123456789ABCDEF",  # Uppercase not allowed
        "0123456789abcdef0123456789abcdeg",  # 'g' is not hex
        "0123456789abcdef/0123456789abcd",
        "0123456789abcdef\\0123456789abcd",
        "0123456789abcdef\x000123456789abc",
        "/absolute/path/to/0123456789abcdef",
    ],
)
def test_invalid_storage_keys_rejected(storage: LocalStorage, invalid_key: str) -> None:
    """Verify malformed, traversal, or invalid-length keys are rejected."""
    assert storage.is_valid_key(invalid_key) is False

    with pytest.raises(StorageInvalidKeyError):
        storage.resolve_path(invalid_key)


# ---------------------------------------------------------------------------
# 2. Initialization and Lifecycle
# ---------------------------------------------------------------------------


async def test_initialize_creates_directory_safely(tmp_path: Path) -> None:
    """Verify initialize creates non-existent storage root safely."""
    new_dir = tmp_path / "deep" / "nested" / "storage"
    backend = LocalStorage(root_path=new_dir)
    assert not new_dir.exists()
    await backend.initialize()
    assert new_dir.exists()
    assert new_dir.is_dir()


async def test_initialize_fails_on_unwritable_directory(tmp_path: Path) -> None:
    """Verify initialize raises StoragePermissionError if directory is not writable."""
    target_dir = tmp_path / "readonly_store"
    target_dir.mkdir(parents=True, exist_ok=True)
    backend = LocalStorage(root_path=target_dir)

    with patch("pathlib.Path.touch", side_effect=PermissionError("Permission denied")):
        with pytest.raises(StoragePermissionError):
            await backend.initialize()


async def test_initialize_fails_on_os_error(tmp_path: Path) -> None:
    """Verify initialize translates unexpected filesystem errors into StorageConfigurationError."""
    target_dir = tmp_path / "error_store"
    target_dir.mkdir(parents=True, exist_ok=True)
    backend = LocalStorage(root_path=target_dir)

    with patch("pathlib.Path.touch", side_effect=OSError("Hardware error")):
        with pytest.raises(StorageConfigurationError):
            await backend.initialize()


# ---------------------------------------------------------------------------
# 3. Basic CRUD Operations
# ---------------------------------------------------------------------------


async def test_save_read_exists_delete_lifecycle(storage: LocalStorage) -> None:
    """Verify the full lifecycle: save, check existence, read, metadata, delete."""
    content = b"Hello, secure storage world!"
    stream = io.BytesIO(content)

    # Save
    stored = await storage.save(stream=stream)
    key = stored.storage_key
    assert len(key) == 32
    assert stored.size_bytes == len(content)
    assert stored.checksum_sha256 == hashlib.sha256(content).hexdigest()

    # Exists
    assert await storage.exists(key) is True

    # Metadata retrieval
    retrieved_meta = await storage.get_metadata(key)
    assert retrieved_meta.storage_key == key
    assert retrieved_meta.size_bytes == len(content)

    # Open and Read
    reader = await storage.open(key)
    assert reader.metadata.storage_key == key
    assert reader.metadata.size_bytes == len(content)

    read_data = bytearray()
    async for chunk in reader.chunks():
        read_data.extend(chunk)
    assert bytes(read_data) == content

    # Delete
    deleted = await storage.delete(key)
    assert deleted is True

    # Verify gone
    assert await storage.exists(key) is False
    with pytest.raises(StorageObjectNotFoundError):
        await storage.get_metadata(key)

    with pytest.raises(StorageObjectNotFoundError):
        await storage.open(key)


async def test_save_empty_file(storage: LocalStorage) -> None:
    """Verify saving an empty stream works correctly with zero size and valid sha256."""
    empty_stream = io.BytesIO(b"")
    stored = await storage.save(stream=empty_stream)

    assert stored.size_bytes == 0
    assert stored.checksum_sha256 == hashlib.sha256(b"").hexdigest()

    reader = await storage.open(stored.storage_key)
    chunks = [chunk async for chunk in reader.chunks()]
    assert chunks == [] or b"".join(chunks) == b""


async def test_save_binary_data(storage: LocalStorage) -> None:
    """Verify binary data with null bytes and non-ASCII characters is preserved."""
    binary_content = bytes(range(256)) * 4  # 1024 bytes
    stream = io.BytesIO(binary_content)

    stored = await storage.save(stream=stream)
    assert stored.size_bytes == 1024
    assert stored.checksum_sha256 == hashlib.sha256(binary_content).hexdigest()

    reader = await storage.open(stored.storage_key)
    read_back = await reader.read()
    assert read_back == binary_content


async def test_delete_nonexistent_behavior(storage: LocalStorage) -> None:
    """Verify deleting a nonexistent key raises error unless missing_ok=True."""
    valid_nonexistent_key = "0" * 32

    with pytest.raises(StorageObjectNotFoundError):
        await storage.delete(valid_nonexistent_key, missing_ok=False)

    assert await storage.delete(valid_nonexistent_key, missing_ok=True) is False


# ---------------------------------------------------------------------------
# 4. Overwrite Protection
# ---------------------------------------------------------------------------


async def test_save_with_existing_key_raises_error(storage: LocalStorage) -> None:
    """Verify attempting to save with a key that already exists raises
    StorageObjectAlreadyExistsError.
    """
    custom_key = "a" * 32
    await storage.save(
        stream=io.BytesIO(b"Initial content"),
        storage_key=custom_key,
    )

    with pytest.raises(StorageObjectAlreadyExistsError):
        await storage.save(
            stream=io.BytesIO(b"New content"),
            storage_key=custom_key,
        )


# ---------------------------------------------------------------------------
# 5. Streaming and Bounded Memory Behavior
# ---------------------------------------------------------------------------


async def test_large_file_chunked_streaming(storage: LocalStorage) -> None:
    """Verify writing and reading a file spanning multiple chunks."""
    # Chunk size is 1024; create 3500 bytes (spanning ~4 chunks)
    data = b"x" * 3500
    assert len(data) == 3500

    stream = io.BytesIO(data)
    stored = await storage.save(stream=stream)
    assert stored.size_bytes == 3500

    reader = await storage.open(stored.storage_key)
    received_chunks: list[bytes] = []
    async for chunk in reader.chunks():
        assert len(chunk) <= 1024
        received_chunks.append(chunk)

    assert len(received_chunks) == 4
    assert b"".join(received_chunks) == data


async def test_sync_chunks_iteration(storage: LocalStorage) -> None:
    """Verify synchronous chunk iteration via sync_chunks."""
    data = b"synchronous streaming test data" * 50
    stream = io.BytesIO(data)
    stored = await storage.save(stream=stream)

    reader = await storage.open(stored.storage_key)
    chunks = list(reader.sync_chunks())
    assert b"".join(chunks) == data


async def test_async_iterator_input_stream(storage: LocalStorage) -> None:
    """Verify save accepts an AsyncIterator[bytes] stream."""

    async def async_generator() -> AsyncIterator[bytes]:
        yield b"chunk one; "
        yield b"chunk two; "
        yield b"chunk three."

    stored = await storage.save(stream=async_generator())
    assert stored.size_bytes == len(b"chunk one; chunk two; chunk three.")

    reader = await storage.open(stored.storage_key)
    content = await reader.read()
    assert content == b"chunk one; chunk two; chunk three."


async def test_sync_iterator_input_stream(storage: LocalStorage) -> None:
    """Verify save accepts a regular Iterator[bytes] stream."""

    def sync_generator() -> Iterator[bytes]:
        yield b"sync part 1; "
        yield b"sync part 2."

    stored = await storage.save(stream=sync_generator())
    reader = await storage.open(stored.storage_key)
    content = await reader.read()
    assert content == b"sync part 1; sync part 2."


class NonSeekableStream:
    """Simulates a non-seekable file-like stream (such as a network pipe or socket)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


async def test_non_seekable_stream_save(storage: LocalStorage) -> None:
    """Verify save works seamlessly with non-seekable streams."""
    data = b"Testing non-seekable streaming capabilities" * 20
    stream = NonSeekableStream(data)

    # Note: NonSeekableStream has no seek or tell attributes
    assert not hasattr(stream, "seek")
    assert not hasattr(stream, "tell")

    stored = await storage.save(stream=stream)
    assert stored.size_bytes == len(data)

    reader = await storage.open(stored.storage_key)
    content = await reader.read()
    assert content == data


# ---------------------------------------------------------------------------
# 6. Atomic Write and Partial Write Cleanup
# ---------------------------------------------------------------------------


class FailingStream:
    """Stream that fails halfway through reading."""

    def __init__(self) -> None:
        self.call_count = 0

    def read(self, size: int = -1) -> bytes:
        self.call_count += 1
        if self.call_count > 1:
            raise OSError("Simulated connection or disk failure mid-stream")
        return b"Initial good bytes that are written before failure"


async def test_partial_write_cleaned_up_on_failure(
    storage: LocalStorage, storage_dir: Path
) -> None:
    """Verify that when an error occurs during write, temporary files are removed."""
    stream = FailingStream()
    key = storage.generate_key()

    with pytest.raises(StorageWriteError) as exc_info:
        await storage.save(stream=stream, storage_key=key)

    # Verify failure occurred
    assert "Failed to persist storage object" in str(exc_info.value)

    # Verify the target file was NOT created
    assert await storage.exists(key) is False

    # Verify NO temporary files (.tmp_*) remain anywhere in the storage tree
    tmp_files = list(storage_dir.rglob(".tmp_*"))
    assert tmp_files == []


# ---------------------------------------------------------------------------
# 7. Resource Cleanup and Context Managers
# ---------------------------------------------------------------------------


async def test_reader_context_manager_cleanup(storage: LocalStorage) -> None:
    """Verify async and sync context manager usage closes the reader cleanly."""
    content = b"Context manager test"
    stored = await storage.save(stream=io.BytesIO(content))

    reader = await storage.open(stored.storage_key)
    async with reader as r:
        chunk = await r.read()
        assert chunk == content
        assert not r.closed

    assert reader.closed

    # Further operations on closed reader raise StorageReadError
    with pytest.raises(StorageReadError):
        await reader.read()


async def test_reader_sync_context_manager(storage: LocalStorage) -> None:
    """Verify sync context manager works as well."""
    stored = await storage.save(stream=io.BytesIO(b"Sync CM"))
    reader = await storage.open(stored.storage_key)

    with reader as r:
        assert not r.closed
        chunks = list(r.sync_chunks())
        assert b"".join(chunks) == b"Sync CM"

    assert reader.closed


# ---------------------------------------------------------------------------
# 8. Security Tests: Path Traversal, Symlinks, and Escape
# ---------------------------------------------------------------------------


def test_path_traversal_attempts(storage: LocalStorage) -> None:
    """Verify path traversal sequences are rejected."""
    traversal_keys = [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "../../storage_data/secret",
        "0123456789abcdef/../../etc/pass",
    ]
    for key in traversal_keys:
        assert storage.is_valid_key(key) is False
        with pytest.raises(StorageInvalidKeyError):
            storage.resolve_path(key)


def test_symlink_escape_detection(storage: LocalStorage, tmp_path: Path) -> None:
    """Verify symlink pointing outside storage root raises StorageSecurityError."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "target.txt"
    outside_file.write_text("Secret external content")

    valid_key = storage.generate_key()
    resolved = storage.resolve_path(valid_key)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Create a symlink at the resolved path pointing outside
        os.symlink(outside_file, resolved)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment/privilege level")

    # Accessing this object must raise StorageSecurityError
    with pytest.raises(StorageSecurityError):
        storage.resolve_path(valid_key)


# ---------------------------------------------------------------------------
# 9. Clean Empty Directories Behavior
# ---------------------------------------------------------------------------


async def test_directory_cleanup_preserves_sibling_files(storage: LocalStorage) -> None:
    """Verify deleting one file does not delete parent directory if it has siblings."""
    # Create two keys that share the same first 4 characters (e.g., '12' and '34')
    prefix = "1234"
    key1 = prefix + "a" * 28
    key2 = prefix + "b" * 28

    await storage.save(stream=io.BytesIO(b"File 1"), storage_key=key1)
    await storage.save(stream=io.BytesIO(b"File 2"), storage_key=key2)

    path1 = storage.resolve_path(key1)
    path2 = storage.resolve_path(key2)
    assert path1.parent == path2.parent

    # Delete first file
    await storage.delete(key1)
    assert not path1.exists()
    assert path2.exists()
    assert path1.parent.exists()  # Parent directory remains because key2 is still there

    # Delete second file
    await storage.delete(key2)
    assert not path2.exists()
    assert not path1.parent.exists()  # Now empty, so it was cleaned up


# ---------------------------------------------------------------------------
# 10. Concurrency: Atomic No-Overwrite Guarantee
# ---------------------------------------------------------------------------


async def test_concurrent_writes_same_key_single_winner(
    storage: LocalStorage, storage_dir: Path
) -> None:
    """Verify that multiple simultaneous writes with the same storage key result
    in exactly one successful write, all others fail with StorageObjectAlreadyExistsError,
    and no temporary files are leaked.
    """
    key = storage.generate_key()
    concurrency_count = 10

    async def _write_worker(worker_id: int) -> tuple[int, object]:
        payload = f"Worker payload {worker_id}".encode()
        try:
            stored = await storage.save(
                stream=io.BytesIO(payload),
                storage_key=key,
            )
            return (worker_id, stored)
        except Exception as exc:
            return (worker_id, exc)

    results = await asyncio.gather(*[_write_worker(i) for i in range(concurrency_count)])

    successes = [r for r in results if not isinstance(r[1], Exception)]
    failures = [r for r in results if isinstance(r[1], Exception)]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(failures) == concurrency_count - 1

    # Every failure must be StorageObjectAlreadyExistsError
    for _, exc in failures:
        assert isinstance(exc, StorageObjectAlreadyExistsError)

    # Verify that the file content on disk matches the winner's payload
    winning_worker_id = successes[0][0]
    expected_content = f"Worker payload {winning_worker_id}".encode()

    reader = await storage.open(key)
    read_back = await reader.read()
    assert read_back == expected_content

    # Verify zero temporary files remain
    tmp_files = list(storage_dir.rglob(".tmp_*"))
    assert tmp_files == []


# ---------------------------------------------------------------------------
# 11. Symlink and Path Security: Partition and File Escapes
# ---------------------------------------------------------------------------


def test_symlink_inside_partition_dir_rejected(storage: LocalStorage, tmp_path: Path) -> None:
    """Verify that a symlink located within intermediate partition directories
    pointing outside the storage root is detected and raises StorageSecurityError.
    """
    outside_dir = tmp_path / "sensitive_zone"
    outside_dir.mkdir(parents=True, exist_ok=True)

    # Attempt to create a symlink for a partition folder
    prefix = "ee"
    partition_folder = storage.root_path / prefix
    try:
        os.symlink(outside_dir, partition_folder)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment/privilege level")

    key = prefix + "ff" + "0" * 28
    with pytest.raises(StorageSecurityError):
        storage.resolve_path(key)


async def test_open_and_delete_symlinked_file_rejected(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify that open, delete, and get_metadata refuse to operate on symlinks."""
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("Confidential host data")

    key = storage.generate_key()
    resolved = storage.resolve_path(key)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.symlink(outside_file, resolved)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment/privilege level")

    with pytest.raises(StorageSecurityError):
        await storage.open(key)

    with pytest.raises(StorageSecurityError):
        await storage.get_metadata(key)

    with pytest.raises(StorageSecurityError):
        await storage.delete(key)


# ---------------------------------------------------------------------------
# 12. Bounded-Memory Reading and Chunking
# ---------------------------------------------------------------------------


async def test_read_full_vs_chunked_boundaries(storage: LocalStorage) -> None:
    """Verify distinction between bounded chunk streaming and explicit full read."""
    data = b"0123456789" * 100  # 1000 bytes
    stored = await storage.save(stream=io.BytesIO(data))

    # Bounded chunks with custom chunk size
    reader = await storage.open(stored.storage_key)
    chunks = [c async for c in reader.chunks(chunk_size=150)]
    assert len(chunks) == 7  # 6 * 150 + 100 = 1000
    assert all(len(c) <= 150 for c in chunks)
    assert b"".join(chunks) == data

    # Partial bounded read
    reader2 = await storage.open(stored.storage_key)
    slice1 = await reader2.read(10)
    assert slice1 == b"0123456789"
    slice2 = await reader2.read(5)
    assert slice2 == b"01234"
    reader2.close()

    # Closed reader operations raise StorageReadError
    with pytest.raises(StorageReadError):
        await reader2.read()


# ---------------------------------------------------------------------------
# 13. Atomic No-Overwrite Fallback (Fail-Closed Guarantees)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "err_code",
    [errno.ENOSYS, errno.EOPNOTSUPP, errno.EXDEV],
)
async def test_unsupported_hard_link_fails_closed(
    storage: LocalStorage, storage_dir: Path, err_code: int
) -> None:
    """Verify that when os.link is unsupported, save() fails closed without publishing."""
    test_data = b"Sensitive payload that must not be published via insecure fallback"
    key = storage.generate_key()

    def _mock_link(*args: object, **kwargs: object) -> None:
        raise OSError(err_code, f"Mocked filesystem error: {os.strerror(err_code)}")

    with patch("os.link", side_effect=_mock_link):
        with pytest.raises(StorageUnsupportedFilesystemError) as exc_info:
            await storage.save(io.BytesIO(test_data), storage_key=key)

        assert f"errno={err_code}" in str(exc_info.value)

    # Verify no object was published
    assert await storage.exists(key) is False

    # Verify no staging temporary file remains anywhere in the storage tree
    remaining_files = list(storage_dir.rglob("*"))
    assert not any(f.name.startswith(".tmp_") for f in remaining_files if f.is_file())


async def test_unsupported_hard_link_never_replaces_existing_object(
    storage: LocalStorage,
) -> None:
    """Verify that fallback failure leaves any pre-existing object intact."""
    original_data = b"Original canonical content"
    key = storage.generate_key()
    stored = await storage.save(io.BytesIO(original_data), storage_key=key)
    assert stored.size_bytes == len(original_data)

    new_data = b"Conflicting replacement payload"

    def _mock_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "Hard links not supported")

    with patch("os.link", side_effect=_mock_link):
        with pytest.raises(StorageObjectAlreadyExistsError):
            await storage.save(io.BytesIO(new_data), storage_key=key)

    # Verify original object was NOT replaced or corrupted
    reader = await storage.open(key)
    content = await reader.read()
    assert content == original_data


# ---------------------------------------------------------------------------
# 14. Intermediate Directory TOCTOU Protection & Descriptor Security
# ---------------------------------------------------------------------------


async def test_intermediate_partition1_symlink_rejected(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify that if partition directory 1 (ab) is a symlink, operations fail."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("outside data")

    part1 = "aa"
    symlink_target = storage.root_path / part1
    try:
        os.symlink(outside_dir, symlink_target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment/privilege level")

    key = f"{part1}/bb/{part1}bb" + "0" * 28

    with pytest.raises(StorageSecurityError):
        await storage.save(io.BytesIO(b"data"), storage_key=key)

    with pytest.raises(StorageSecurityError):
        await storage.open(key)

    with pytest.raises(StorageSecurityError):
        await storage.get_metadata(key)

    with pytest.raises(StorageSecurityError):
        await storage.delete(key)

    assert await storage.exists(key) is False


async def test_intermediate_partition2_symlink_rejected(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify that if partition directory 2 (ab/cd) is a symlink, operations fail."""
    outside_dir = tmp_path / "outside_dir2"
    outside_dir.mkdir(parents=True, exist_ok=True)

    part1 = "bb"
    part2 = "cc"
    p1_dir = storage.root_path / part1
    p1_dir.mkdir(parents=True, exist_ok=True)

    symlink_target = p1_dir / part2
    try:
        os.symlink(outside_dir, symlink_target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in this environment/privilege level")

    key = f"{part1}/{part2}/{part1}{part2}" + "1" * 28

    with pytest.raises(StorageSecurityError):
        await storage.save(io.BytesIO(b"data"), storage_key=key)

    with pytest.raises(StorageSecurityError):
        await storage.open(key)

    with pytest.raises(StorageSecurityError):
        await storage.get_metadata(key)

    with pytest.raises(StorageSecurityError):
        await storage.delete(key)

    assert await storage.exists(key) is False


async def test_intermediate_dir_swap_race_during_save_stays_contained(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify descriptor-pinned directory writes cannot be redirected by symlink swaps."""
    if not _SUPPORTS_DIR_FD:
        pytest.skip("Descriptor-relative dir_fd operations not supported on this platform")

    outside_dir = tmp_path / "evil_target_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)

    part1 = "cc"
    part2 = "dd"
    key = f"{part1}/{part2}/{part1}{part2}" + "2" * 28
    target_path = storage.resolve_path(key)

    # Ensure parent partition directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)
    real_parent = target_path.parent

    class RaceTriggerStream:
        """Stream that swaps the partition directory for a symlink during read()."""

        def __init__(self, data: bytes) -> None:
            self._data = data
            self._read = False

        def read(self, size: int = -1) -> bytes:
            if not self._read:
                self._read = True
                # TOCTOU attack simulation: swap partition dir with symlink to outside_dir
                old_dir = real_parent.parent / f"{part2}_backup"
                os.rename(real_parent, old_dir)
                os.symlink(outside_dir, real_parent)
                return self._data
            return b""

    stream = RaceTriggerStream(b"pinned inode content")
    try:
        stored = await storage.save(stream, storage_key=key)
        assert stored.size_bytes == len(b"pinned inode content")

        # Crucial security verification:
        # 1. Target was NOT written into evil_dir!
        evil_contents = list(outside_dir.iterdir())
        assert len(evil_contents) == 0, "Security violation: file created in symlink target!"
    finally:
        # Cleanup swapped symlink
        if real_parent.is_symlink():
            real_parent.unlink()
        old_dir = real_parent.parent / f"{part2}_backup"
        if old_dir.exists():
            os.rename(old_dir, real_parent)


async def test_intermediate_dir_swap_race_during_open_stays_pinned(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify that opening a file reads from pinned inode even if dir is swapped for symlink."""
    if not _SUPPORTS_DIR_FD:
        pytest.skip("Descriptor-relative dir_fd operations not supported on this platform")

    part1 = "dd"
    part2 = "ee"
    key = f"{part1}/{part2}/{part1}{part2}" + "3" * 28
    original_data = b"confidential original payload"
    await storage.save(io.BytesIO(original_data), storage_key=key)

    outside_dir = tmp_path / "evil_dir_open"
    outside_dir.mkdir(parents=True, exist_ok=True)
    evil_file = outside_dir / f"{part1}{part2}{'3' * 28}"
    evil_file.write_bytes(b"MALICIOUS SUBSTITUTED CONTENT")

    target_path = storage.resolve_path(key)
    real_parent = target_path.parent

    import app.storage.local as local_mod

    orig_open_fds = local_mod._open_partition_dir_fds

    import contextlib

    @contextlib.contextmanager
    def _hooked_open_fds(*args: object, **kwargs: object) -> Iterator[tuple[int, int, int]]:
        with orig_open_fds(*args, **kwargs) as fds:  # type: ignore[arg-type]
            # TOCTOU swap after descriptor acquisition
            old_dir = real_parent.parent / f"{part2}_backup"
            os.rename(real_parent, old_dir)
            os.symlink(outside_dir, real_parent)
            try:
                yield fds
            finally:
                pass

    with patch.object(local_mod, "_open_partition_dir_fds", side_effect=_hooked_open_fds):
        try:
            reader = await storage.open(key)
            read_bytes = await reader.read()
            assert read_bytes == original_data
            reader.close()
        finally:
            if real_parent.is_symlink():
                real_parent.unlink()
            old_dir = real_parent.parent / f"{part2}_backup"
            if old_dir.exists():
                os.rename(old_dir, real_parent)


async def test_intermediate_dir_swap_race_during_delete_stays_pinned(
    storage: LocalStorage, tmp_path: Path
) -> None:
    """Verify that deleting a file unlinks pinned inode and never deletes symlink target."""
    if not _SUPPORTS_DIR_FD:
        pytest.skip("Descriptor-relative dir_fd operations not supported on this platform")

    part1 = "ee"
    part2 = "ff"
    key = f"{part1}/{part2}/{part1}{part2}" + "4" * 28
    await storage.save(io.BytesIO(b"data to delete"), storage_key=key)

    outside_dir = tmp_path / "victim_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    victim_file = outside_dir / f"{part1}{part2}{'4' * 28}"
    victim_file.write_bytes(b"CRITICAL VITAL SYSTEM DATA")

    target_path = storage.resolve_path(key)
    real_parent = target_path.parent

    import app.storage.local as local_mod

    orig_open_fds = local_mod._open_partition_dir_fds

    import contextlib

    @contextlib.contextmanager
    def _hooked_open_fds(*args: object, **kwargs: object) -> Iterator[tuple[int, int, int]]:
        with orig_open_fds(*args, **kwargs) as fds:  # type: ignore[arg-type]
            old_dir = real_parent.parent / f"{part2}_backup"
            os.rename(real_parent, old_dir)
            os.symlink(outside_dir, real_parent)
            try:
                yield fds
            finally:
                pass

    with patch.object(local_mod, "_open_partition_dir_fds", side_effect=_hooked_open_fds):
        try:
            deleted = await storage.delete(key)
            assert deleted is True

            # Assert victim file in outside_dir was NOT deleted!
            assert victim_file.exists(), "Security violation: victim file was deleted!"
            assert victim_file.read_bytes() == b"CRITICAL VITAL SYSTEM DATA"
        finally:
            if real_parent.is_symlink():
                real_parent.unlink()
            old_dir = real_parent.parent / f"{part2}_backup"
            if old_dir.exists():
                os.rename(old_dir, real_parent)
