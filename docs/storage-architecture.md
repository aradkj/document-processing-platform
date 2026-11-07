# Storage Architecture & Foundation (Phase 3.1)

## 1. Overview & Objectives

The Storage Architecture in the Document Processing Platform provides an abstract, modular, decoupled, and secure file persistence foundation designed for high-throughput, bounded-memory file streaming.

Initially utilizing local filesystem storage (`LocalStorage`), the storage layer is strictly decoupled from:
- Web framework dependencies (`FastAPI`, `Request`, `UploadFile`, `Response`).
- Database models and ORM sessions (`SQLAlchemy`, `User`, or future document models).
- Document business logic (OCR, processing queues, parsing, classification).

This ensures future S3-compatible object backends (such as AWS S3 or MinIO) can be introduced seamlessly by implementing the standard `StorageBackend` interface without altering business logic or API layers.

---

## 2. Package Architecture & Responsibilities

The storage package is located under `app/storage/`:

```
app/storage/
├── __init__.py        # Factory, singleton cache, dependency injection helpers (get_storage)
├── base.py            # Abstract Base Classes (StorageBackend, StoredObjectReader)
├── exceptions.py      # Encapsulated storage exception hierarchy
├── local.py           # Production LocalStorage implementation with atomic writes & partitioned keys
└── types.py           # Neutral dataclasses (StorageMetadata, StoredObject, BinaryStream protocol)
```

### Component Responsibilities

1. **`app/storage/base.py` (`StorageBackend`, `StoredObjectReader`)**:
   - `StorageBackend`: Abstract base class specifying neutral async contracts: `save()`, `open()`, `delete()`, `exists()`, `get_metadata()`, `generate_key()`, `initialize()`, and `close()`.
   - `StoredObjectReader`: Abstract streaming reader contract supporting both chunked async streaming (`chunks()`), synchronous chunking (`sync_chunks()`), and context managers for deterministic resource disposal.

2. **`app/storage/types.py` (`StoredObject`, `StorageMetadata`, `BinaryStream`)**:
   - `StorageMetadata`: Represents stored object metadata (`storage_key`, `size_bytes`, `created_at`, `modified_at`).
   - `StoredObject`: Data transfer object returned upon successful writes (`storage_key`, `size_bytes`, `checksum_sha256`, `created_at`).
   - `BinaryStream`: Structural typing protocol describing any file-like object exposing a synchronous `read(size: int) -> bytes` method.

3. **`app/storage/exceptions.py`**:
   - Complete exception hierarchy rooted at `StorageError`. Low-level filesystem errors (`OSError`, `PermissionError`, `FileNotFoundError`) are caught internally and re-raised as typed domain exceptions without leaking internal filesystem paths to public consumers.

4. **`app/storage/local.py` (`LocalStorage`, `LocalStoredObjectReader`)**:
   - Filesystem backend managing atomic disk writes, directory partitioning, streaming reads, path sanitization, and partial write cleanups.

5. **`app/storage/__init__.py`**:
   - `create_storage_backend(settings)`: Factory instantiating the configured backend.
   - `get_storage(request)`: FastAPI dependency yielding the application-level storage backend instance attached to `request.app.state.storage`.

---

## 3. Storage Object Identification & Key Design

Original user-supplied filenames are **never** used as physical paths on disk to prevent path traversal, directory collisions, and character encoding exploits.

### Storage Key Format
- Every object is assigned an opaque, cryptographically secure 128-bit random identifier generated via `secrets.token_hex(16)` (32 lowercase hexadecimal characters).
- Example Canonical Key:
  ```
  4f9a7c3b2e1d0f8a9b8c7d6e5f4a3b2c
  ```

### Partitioned On-Disk Layout
To avoid directory scaling degradation (filesystems like ext4 experience inode lookup slowdowns when hundreds of thousands of files exist in a single flat directory), `LocalStorage` partitions keys into a two-level prefix hierarchy:
- Key: `4f9a7c3b2e1d0f8a9b8c7d6e5f4a3b2c`
- Directory path: `<storage_root>/4f/9a/4f9a7c3b2e1d0f8a9b8c7d6e5f4a3b2c`
- Maximum subdirectories at each tier: $16^2 = 256$, providing $256 \times 256 = 65,536$ distributed storage buckets.

### Key Validation, Symlink Safety & Intermediate Directory Security Model
Before resolving paths or accessing disk:
1. Keys are strictly validated against `^[a-f0-9]{32}$` or partitioned `^[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{32}$`.
2. Any string containing `\0`, `..`, `\\`, or uppercase hex is rejected immediately with `StorageInvalidKeyError`.
3. Paths are resolved and strictly verified to reside under `root_path.resolve()` using `Path.is_relative_to()`.

#### POSIX / Linux Descriptor-Relative Directory Traversal (`dir_fd`)
On platforms supporting directory descriptors (`os.supports_dir_fd`), `LocalStorage` eliminates intermediate-directory TOCTOU vulnerabilities through descriptor-relative operations:
- The storage root is opened with `O_RDONLY | O_DIRECTORY`.
- Each partition tier (`part1` = `ab`, `part2` = `cd`) is traversed and opened using `open(part, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=parent_fd)`.
- If an attacker replaces `ab` or `cd` with a symlink before access, the kernel refuses to follow it and immediately raises `ENOTDIR` or `ELOOP`, which `LocalStorage` maps to `StorageSecurityError`.
- Once `p2_fd` is obtained, it pins the directory inode in the kernel. Subsequent operations (`save`, `open`, `delete`, `exists`, `get_metadata`) operate directly on `p2_fd` via `dir_fd` parameters (`open(filename, ..., dir_fd=p2_fd)`, `os.link(src, dst, src_dir_fd=p2_fd, dst_dir_fd=p2_fd)`, `os.unlink(filename, dir_fd=p2_fd)`, `os.stat(filename, dir_fd=p2_fd, follow_symlinks=False)`).
- Even if an attacker swaps the directory on disk after descriptor acquisition, subsequent file operations execute strictly within the original pinned directory inode, eliminating directory substitution TOCTOU races.

#### Windows Platform Boundaries & Limitations
- On Windows, standard Python `os.supports_dir_fd` is empty; `dir_fd`, `O_DIRECTORY`, and `O_NOFOLLOW` are not available via standard POSIX emulation.
- **Operating Guarantees on Windows:** Path validation enforces strict canonical containment (`is_relative_to(root)`), checks `is_symlink()` across intermediate path components, and relies on NTFS atomic hard-link publication via `CreateHardLinkW`.
- **Limitation:** Because directory file descriptors cannot be pinned on Windows, intermediate directories cannot be protected by `dir_fd`-relative pinning. In environments where untrusted local users have write permissions to the storage directory tree, a theoretical Time-of-Check to Time-of-Use race exists on Windows. Therefore, Windows production deployments **must** enforce strict NTFS ACLs restricting write access solely to the application service account.

---

## 4. Streaming, Concurrency & Memory Bounded Operations

To accommodate large documents without unbounded memory consumption and protect data integrity under high concurrency:

### Writes (`save`)
- Accepts standard binary file streams (`BinaryIO`), generic file-like objects (`BinaryStream`), asynchronous iterators (`AsyncIterator[bytes]`), or synchronous generators (`Iterator[bytes]`).
- Writes in bounded chunks (default `1,048,576` bytes = 1MB).
- Synchronous I/O operations are offloaded to worker threads via `asyncio.to_thread` to preserve FastAPI event loop responsiveness.
- Simultaneously computes SHA-256 digest and tracks byte counts on-the-fly without requiring seekable streams.

### Atomic Publication Guarantees & Fallback Behavior
1. Incoming streams are initially written to an isolated temporary staging file (`.tmp_<random>_<name>`) located directly within the target partition.
2. If stream reading is interrupted, aborted, or encounters an I/O error, the temporary file is unlinked immediately in an exception handler, and `StorageWriteError` is raised. The target file is never created.
3. Publication is performed using an atomic hard link (`os.link`):
   - **Supported Filesystems:** POSIX filesystems (ext4, XFS, ZFS, Btrfs) and Windows NTFS natively support hard links within the same filesystem partition.
   - **Single Winner Guarantee:** Competing concurrent writers attempting to save under the exact same key race at the kernel `link(2)` / `CreateHardLinkW` boundary. Exactly one writer succeeds. Competing concurrent writers receive `FileExistsError` (or `errno.EEXIST`), immediately clean up their staging files, and raise `StorageObjectAlreadyExistsError`. Existing objects are never overwritten.
   - **Fail-Closed on Unsupported Filesystems:** If the underlying storage filesystem does not support hard links (`errno.ENOSYS`, `errno.EOPNOTSUPP`, or cross-filesystem `errno.EXDEV`, such as FAT32, exFAT, or certain network mounts):
     - The backend **fails closed** by cleaning up the staging file and raising `StorageUnsupportedFilesystemError`.
     - **No Check-Then-Act Fallback:** The system intentionally does **NOT** fall back to `exists()` followed by `os.replace` or `os.rename`. Such check-then-act sequences introduce severe TOCTOU overwrite races under concurrency and would silently downgrade security guarantees.
     - Filesystem compatibility requirements must be resolved at the infrastructure level (e.g., using standard ext4/XFS storage volumes).

### Reads (`open`)
- `storage.open(storage_key)` returns a `StoredObjectReader`.
- **Bounded-Memory Streaming (`chunks()`):** The primary access pattern for large documents. Yields byte chunks up to `chunk_size` (default 1MB), maintaining flat, bounded memory consumption even for multi-gigabyte files.
- **Explicit Full-Content Read (`read(size=-1)`):** Reads up to `size` bytes. If `size` is negative or omitted, all remaining content is loaded into memory at once. Callers must note that `read(-1)` uses memory proportional to file size; for bounded memory processing of large files, use `chunks()` instead.
- File descriptors are automatically closed upon generator exhaustion or via async/sync context managers:
  ```python
  async with await storage.open(key) as reader:
      async for chunk in reader.chunks():
          ...
  ```

---

## 5. Error Handling & Exception Hierarchy

```
StorageError
├── StorageConfigurationError
│   └── StorageUnsupportedFilesystemError
├── StorageInvalidKeyError
├── StorageObjectNotFoundError
├── StorageObjectAlreadyExistsError
├── StoragePermissionError
├── StorageWriteError
├── StorageReadError
├── StorageDeleteError
└── StorageSecurityError
```

- **Path Masking**: Low-level exceptions (`OSError`, `PermissionError`, `FileNotFoundError`) are encapsulated. Error messages never disclose absolute filesystem roots to callers.
- **Unsupported Filesystems**: `StorageUnsupportedFilesystemError` explicitly informs administrators when the storage volume lacks required atomic hard-link publication support.
- **Missing Object Contracts**:
  - `get_metadata(key)` raises `StorageObjectNotFoundError` if missing.
  - `open(key)` raises `StorageObjectNotFoundError` if missing.
  - `delete(key, missing_ok=False)` raises `StorageObjectNotFoundError` if missing.
  - `delete(key, missing_ok=True)` returns `False` safely without error.
  - `exists(key)` returns `False` safely for nonexistent or malformed keys.

---

## 6. Configuration & Lifecycle Integration

### Configuration Settings (`app/core/config.py`)
- `STORAGE_BACKEND`: Selected backend (defaults to `"local"`).
- `STORAGE_LOCAL_ROOT`: Storage root path (defaults to `"./storage_data"`).
- `STORAGE_CHUNK_SIZE`: Streaming buffer size in bytes (defaults to `1048576`, validated between 64KB and 16MB).
- Production Safety: In `production`, setting `STORAGE_LOCAL_ROOT` to root (`/`), `/etc`, `/bin`, etc., is rejected during settings validation.

### Application Lifespan (`app/main.py`)
- At application startup (`lifespan`), `create_storage_backend(settings)` creates the storage backend.
- `await storage.initialize()` verifies directory existence and executes a safe write-probe test (`.probe_<token>`). If unwriteable, startup halts fail-fast with `StoragePermissionError`.
- Attached to `app.state.storage = storage` for dependency injection.
- At shutdown, `await storage.close()` cleanly releases any retained backend resources.

---

## 7. Docker Persistence & Security

### Volume Mapping (`docker-compose.yml`)
- A dedicated named volume `storage_data` is mounted to `/app/storage_data` in the `api` container.
- Configured via environment variable: `STORAGE_LOCAL_ROOT=/app/storage_data`.

### Non-Root User Permissions (`Dockerfile`)
- In `Dockerfile`, `/app/storage_data` is pre-created and ownership assigned to `appuser:appgroup` before dropping root privileges (`USER appuser`).
- Storage data persists across container re-creation and restarts.
- Not exposed to the public network or host static web directories.

---

## 8. Future Extension: S3-Compatible Storage

To integrate AWS S3 or MinIO in future phases:
1. Implement `S3Storage(StorageBackend)` implementing all methods in `app/storage/base.py`.
2. Add `"s3"` to `STORAGE_BACKEND` Literal in `Settings`.
3. Add S3 credentials (`STORAGE_S3_BUCKET`, `STORAGE_S3_ENDPOINT`, `STORAGE_S3_ACCESS_KEY`, `STORAGE_S3_SECRET_KEY`) under `Settings`.
4. Register backend in `create_storage_backend(settings)` factory.
5. All downstream services and endpoints consuming `StorageBackend` require zero code modifications.
