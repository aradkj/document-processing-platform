# Document Architecture & Database Foundation (Phase 3.2)

## 1. Overview & Purpose

Phase 3.2 introduces the database foundation for document management in the Document Processing Platform. It establishes:
- The `Document` SQLAlchemy domain model representing document metadata and ownership.
- PostgreSQL database-level constraints enforcing data integrity, secure key formats, and non-negative file sizing.
- Strategic B-tree indexes supporting fast owner-scoped listing and pagination without duplicate index overhead.
- An explicit user deletion policy (`ON DELETE RESTRICT`) preventing physical file orphaning on external storage.
- Alembic migration `0010_create_documents_table` tracking schema evolution.

Physical document file contents remain strictly stored outside PostgreSQL within the `StorageBackend` abstraction (Phase 3.1). PostgreSQL stores only metadata, integrity checksums, and ownership links.

---

## 2. Separation of PostgreSQL Metadata and Physical File Storage

The architecture enforces strict separation of concerns between storage tiers:

```
+-----------------------------------+        +-----------------------------------+
|         PostgreSQL Database       |        |      StorageBackend (Phase 3.1)   |
|         (documents Table)         |        |         (Local Filesystem)        |
+-----------------------------------+        +-----------------------------------+
| id: UUID                          |        | Root: app_data/storage/documents/ |
| user_id: UUID (FK -> users.id)    |        | Partition: 4f/8d/                 |
| storage_key: VARCHAR(32) (opaque) | -----> | File: 4f8d9b2e1c3a4e5b6c7d8e9f... |
| original_filename: VARCHAR(255)   |        | Contents: Raw binary stream bytes |
| content_type: VARCHAR(100)        |        +-----------------------------------+
| size_bytes: BIGINT                |
| sha256: VARCHAR(64)               |
| created_at / updated_at: TIMESTAMPTZ
+-----------------------------------+
```

### Architectural Guarantees:
1. **No Binary Content in PostgreSQL**: Document payloads are never stored as `BYTEA` or `BLOB` inside PostgreSQL.
2. **Decoupled Identity**: A document's logical identity (`id: UUID`) is completely independent of its physical storage location (`storage_key: VARCHAR(32)`).
3. **No Filesystem Operations in ORM**: The `Document` model stores the opaque `storage_key`, but never directly invokes filesystem or I/O calls.
4. **Metadata-Only Display Names**: `original_filename` is strictly user-facing display metadata and is never used to construct storage paths.

---

## 3. Document Entity & Metadata Fields

The `Document` model is defined in `app/db/models/document.py` inheriting from `app.db.base.Base`.

| Field Name | PostgreSQL Type | Nullable | Constraints & Defaults | Description |
|---|---|---|---|---|
| `id` | `UUID` | No | Primary Key, `uuid.uuid4()` default | Logical unique identifier for document entity. |
| `user_id` | `UUID` | No | Foreign Key to `users.id`, `ON DELETE RESTRICT` | Owning user account UUID. |
| `storage_key` | `VARCHAR(32)` | No | `UNIQUE`, `CHECK (~ '^[0-9a-f]{32}$')` | Opaque 32-character lowercase hex identifier for StorageBackend retrieval. |
| `original_filename` | `VARCHAR(255)` | No | None | User-submitted original filename for display and download naming. |
| `content_type` | `VARCHAR(100)` | No | None | Validated MIME/media type (e.g. `application/pdf`, `image/png`). |
| `size_bytes` | `BIGINT` | No | `CHECK (size_bytes >= 0)` | Stored binary payload size in bytes. 64-bit integer supports files > 2 GB. |
| `sha256` | `VARCHAR(64)` | No | `CHECK (~ '^[0-9a-f]{64}$')` | Hexadecimal SHA-256 digest calculated over stored binary stream. |
| `created_at` | `TIMESTAMPTZ` | No | `server_default=now()` | UTC creation timestamp. |
| `updated_at` | `TIMESTAMPTZ` | No | `server_default=now()`, auto-updated on change | UTC last modification timestamp. |

---

## 4. Relationship with User & Deletion Policy

### Relationship Semantics
- **One-to-Many**: A `User` can own zero, one, or multiple `Document` entities.
- **Single Owner**: Every `Document` belongs to exactly one `User`. Anonymous or unowned documents cannot exist (`user_id` is non-nullable).
- **Async Safety**: Both sides of the relationship (`User.documents` and `Document.user`) specify `lazy="raise"` to prevent implicit synchronous I/O or greenlet exceptions in async FastAPI endpoints.
- **Eager Loading**: Endpoints query documents using `selectinload(User.documents)` when document relations are required.

### User Deletion Policy: `ON DELETE RESTRICT`
Physical files reside outside PostgreSQL on disk. If a User record could be deleted via database cascade (`ON DELETE CASCADE`), the metadata row in `documents` would be silently wiped while the physical binary object remained abandoned on disk with no owner or reference.

To protect against physical file orphaning:
1. **Foreign Key Constraint**: `fk_documents_user_id_users` specifies `ON DELETE RESTRICT`.
2. **ORM Configuration**: `User.documents` specifies `passive_deletes="all"` with no `cascade="all, delete-orphan"` or delete cascades. Setting `passive_deletes="all"` prevents SQLAlchemy from attempting in-memory foreign key nullification (`UPDATE documents SET user_id=NULL`) when deleting a user whose documents are loaded in the session, allowing PostgreSQL's `ON DELETE RESTRICT` constraint to be evaluated directly.
3. **Behavior**: Deleting a User who owns documents is strictly rejected by PostgreSQL with an `IntegrityError`.
4. **Lifecycle Workflow**: In future phases, document deletion requires an explicit orchestration service that first verifies ownership, deletes the physical file from `StorageBackend`, and subsequently removes the PostgreSQL metadata record. A user can only be deleted once all owned documents have been explicitly removed.

---

## 5. Database Constraints & Integrity Guarantees

PostgreSQL-level constraints guarantee data integrity independently of API or Pydantic validation:

1. **`ck_documents_size_bytes_non_negative`**:
   - `size_bytes >= 0`
   - Prevents corrupted or negative file sizes from entering the database.
2. **`ck_documents_storage_key_hex32`**:
   - `storage_key ~ '^[0-9a-f]{32}$'`
   - Validates that every storage key is an exact 32-character lowercase hexadecimal string, matching the format generated by `secrets.token_hex(16)` in Phase 3.1.
   - Prevents path traversal patterns (`..`), backslashes, uppercase characters, or invalid encodings from being stored.
3. **`ck_documents_sha256_hex64`**:
   - `sha256 ~ '^[0-9a-f]{64}$'`
   - Validates that all checksums are exactly 64-character lowercase hexadecimal strings representing valid SHA-256 digests.
4. **`uq_documents_storage_key`**:
   - Ensures no two document records can point to the same physical storage object.
5. **`fk_documents_user_id_users`**:
   - Foreign key constraint enforcing referential integrity with `users.id` and restricting parent user deletion while documents exist.

---

## 6. Index Strategy & Access Patterns

Indexes are tailored directly to the platform's query patterns while adhering to the anti-duplication guidelines demonstrated in migration `0008`:

| Index Name | Target Columns | Index Type | Purpose & Access Pattern |
|---|---|---|---|
| `pk_documents` | `id` | B-tree (Unique) | Primary key index for document retrieval by UUID. |
| `uq_documents_storage_key` | `storage_key` | B-tree (Unique) | Created by unique constraint. O(1) lookups by storage key. Avoids redundant standalone index. |
| `ix_documents_user_id_created_at` | `(user_id, created_at)` | B-tree | Composite index supporting owner queries (`WHERE user_id = :uid`) and ordered paginated listing (`ORDER BY created_at [DESC/ASC]`). |

### Avoidance of Redundant Indexes:
- In B-tree indexes, queries filtering on the leftmost prefix `user_id` are efficiently handled by `ix_documents_user_id_created_at`.
- A standalone index on `(user_id)` was intentionally omitted to prevent index duplication and write amplification, matching the optimization pattern implemented in migration `0008_optimize_email_token_idx`.

---

## 7. Alembic Migration `0010_create_documents_table`

- **Identifier**: `0010_create_documents_table`
- **Down Revision**: `0009_password_reset_tokens`
- **Upgrade**: Creates the `documents` table, primary key, foreign key, unique constraint, check constraints, and composite index.
- **Downgrade**: Drops the composite index and drops the `documents` table cleanly, preserving all Phase 1 and Phase 2 authentication tables and records.

---

## 8. Scope Boundaries & Phase 3 Roadmap

Phase 3 establishes the document storage and management subsystem across distinct sub-phases:
- **Phase 3.1 — Storage Architecture & Local Filesystem Foundation (Completed)**: Abstract `StorageBackend` interface, `LocalStorage` implementation with opaque 32-hex keys, two-level directory partitioning, atomic hard-link publication (`os.link`), chunked streaming I/O, and traversal security.
- **Phase 3.2 — Document Data Model & Database (Completed)**: `Document` SQLAlchemy domain model, PostgreSQL integrity constraints (key format, checksum format, non-negative size, unique keys), composite B-tree indexing `(user_id, created_at)`, `ON DELETE RESTRICT` with `passive_deletes="all"`, and Alembic migration `0010_create_documents_table`.
- **Phase 3.3 — Secure Document Upload (Completed)**: HTTP upload endpoints (`POST /api/v1/documents`), multipart streaming into storage staging, magic-byte MIME type validation, file size quota enforcement, and metadata persistence in PostgreSQL with storage compensation on commit failure.
- **Phase 3.4 — Document Listing & Management (Next)**: Document query endpoints (`GET /api/v1/documents`, `GET /api/v1/documents/{id}`), metadata filtering, pagination, and ownership authorization.
- **Phase 3.5 — Secure Download & Coordinated Deletion**: Streaming download endpoints (`GET /api/v1/documents/{id}/download`), Range header support, and coordinated two-phase deletion (atomic physical file unlink via `StorageBackend` followed by database metadata record removal).

The following items are deferred to Phase 4 and later:
- Document processing pipeline, OCR execution, and text extraction (Phase 4).
- Background task queues (Celery / RabbitMQ / Redis).
- Document full-text search, semantic tagging, and document sharing.

---

## 9. Secure Document Upload Subsystem (Phase 3.3)

### 9.1 Upload Sequence & Component Architecture

Phase 3.3 connects the HTTP presentation layer (`POST /api/v1/documents`), validation services, decoupled `StorageBackend`, and PostgreSQL `Document` metadata persistence.

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI /api/v1/documents
    participant Auth as get_current_user
    participant Val as Validation Service
    participant Storage as StorageBackend (LocalStorage)
    participant DB as PostgreSQL (AsyncSession)

    Client->>API: POST /api/v1/documents (multipart/form-data)
    API->>Auth: Verify JWT Bearer token
    Auth-->>API: Authenticated User (user.id)
    API->>Val: validate_and_sanitize_filename(file.filename)
    Val-->>API: Normalized filename (NFC)
    API->>Val: Read initial chunk (64KB) & detect_content_type(chunk)
    Val-->>API: Validated MIME type (PDF, PNG, JPEG, TIFF, WebP)
    API->>Val: validate_extension_matches_content(filename, content_type)
    Val-->>API: Validation Passed
    API->>Storage: save(storage_key, chunk_generator)
    Note over Storage: Stream chunks (1MB) to staging file;<br/>incremental SHA-256 & byte counting;<br/>enforce MAX_UPLOAD_SIZE_BYTES;<br/>atomic hard-link publication
    Storage-->>API: StorageMetadata (storage_key, size_bytes, sha256)
    API->>DB: INSERT INTO documents (id, user_id, storage_key, ...)
    alt Commit Succeeded
        DB-->>API: Commit OK
        API-->>Client: 201 Created (DocumentResponse)
    else Commit Failed (Integrity / DB Error)
        DB-->>API: Exception (Rollback)
        API->>Storage: delete(storage_key, missing_ok=True) [Compensation]
        API-->>Client: 500 Internal Server Error
    end
```

### 9.2 Filename Validation & Normalization
Filenames provided by clients are untrusted and must never be used to construct filesystem paths or executed in shell environments. `validate_and_sanitize_filename` enforces:
1. **Unicode Normalization**: Canonical Decomposition followed by Canonical Composition (NFC) via `unicodedata.normalize("NFC", filename)`.
2. **Prohibited Characters**: Rejection of null bytes (`\0` and encoded `%00`), path traversal sequences (`..`), directory separators (`/` and `\\`), Windows drive colons (`:`), and non-printable control characters (`\x00`–`\x1f`, `\x7f`–`\x9f`).
3. **Structure & Length**: Enforces non-empty filenames, maximum length of 255 characters, presence of both a non-empty stem and a non-empty extension, and rejection of dot-only names (`.` or `..`).

### 9.3 Trustworthy Content-Type Detection (Magic Bytes)
Client-provided `Content-Type` headers are completely ignored for security decisions. Instead, `detect_content_type` inspects the initial byte signature of the file payload:
- **PDF**: Begins with `%PDF-` (`0x25 0x50 0x44 0x46 0x2D`).
- **JPEG**: Begins with `\xFF\xD8\xFF`.
- **PNG**: Begins with `\x89PNG\r\n\x1a\n` (`0x89 0x50 0x4E 0x47 0x0D 0x0A 0x1A 0x0A`).
- **TIFF**: Begins with Little-Endian `II*\x00` (`0x49 0x49 0x2A 0x00`) or Big-Endian `MM\x00*` (`0x4D 0x4D 0x00 0x2A`).
- **WebP**: 12-byte container signature starting with `RIFF` at offset 0 and `WEBP` at offset 8.

Files with unknown signatures trigger `415 Unsupported Media Type`. Files whose extension does not match the detected binary format (e.g., an executable renamed to `.pdf` or a PNG renamed to `.jpg`) are rejected immediately via `validate_extension_matches_content`.

### 9.4 Bounded-Memory Streaming & Quota Enforcement
To prevent denial-of-service via memory exhaustion:
1. The file stream is consumed asynchronously using an async generator yielding chunks (default 1MB).
2. The initial chunk (up to 64KB) is read first to inspect magic bytes and detect content type.
3. If the initial chunk exceeds `MAX_UPLOAD_SIZE_BYTES`, the upload is aborted immediately with `FileTooLargeError` without invoking storage writes.
4. The async generator passes chunks directly to `storage.save(key, stream)`.
5. As chunks are read, cumulative byte count is tracked. If the total size exceeds `MAX_UPLOAD_SIZE_BYTES`, streaming halts immediately and partial staging files are unlinked by `LocalStorage`.

### 9.5 Transactional Consistency & Two-Phase Compensation
A distributed state discrepancy can arise if file bytes are written to physical storage but the database transaction fails to commit (e.g., connection drop, timeout, or constraint violation).
- **Failure Compensation**: The upload service wraps database creation in a try/except block. If `session.commit()` fails, the database transaction is rolled back and `storage.delete(storage_key, missing_ok=True)` is immediately invoked to delete the newly written physical file.
- **Detached Instance Safety**: Prior to database operations, the user's primary key (`user_id = user.id`) is copied to a local variable to prevent SQLAlchemy `DetachedInstanceError` when logging or handling errors post-rollback.
- **Resource Cleanup**: The Starlette `UploadFile` handle is guaranteed to be closed in a `finally` block to delete any underlying spool temporary files created by the ASGI server.

