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
- **Phase 3.4 — Document Listing & Management (Completed)**: Document query endpoints (`GET /api/v1/documents`, `GET /api/v1/documents/{id}`), metadata filtering, pagination, and ownership authorization.
- **Phase 3.5 — Secure Download & Coordinated Deletion (Next)**: Streaming download endpoints (`GET /api/v1/documents/{id}/download`), Range header support, and coordinated two-phase deletion (atomic physical file unlink via `StorageBackend` followed by database metadata record removal).

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

### 9.3 Content-Type Detection (Magic Bytes) & Security Boundaries
Client-provided `Content-Type` headers are completely ignored for security decisions. Instead, `detect_content_type` inspects the initial byte signature of the file payload against an explicitly supported format allowlist:
- **PDF**: Begins with `%PDF-` (`0x25 0x50 0x44 0x46 0x2D`).
- **JPEG**: Begins with `\xFF\xD8\xFF`.
- **PNG**: Begins with `\x89PNG\r\n\x1a\n` (`0x89 0x50 0x4E 0x47 0x0D 0x0A 0x1A 0x0A`).
- **TIFF**: Begins with Little-Endian `II*\x00` (`0x49 0x49 0x2A 0x00`) or Big-Endian `MM\x00*` (`0x4D 0x4D 0x00 0x2A`).
- **WebP**: 12-byte container signature starting with `RIFF` at offset 0 and `WEBP` at offset 8.

Files with unknown signatures trigger `415 Unsupported Media Type`. Files whose extension does not match the detected binary format (e.g., an executable renamed to `.pdf` or a PNG renamed to `.jpg`) are rejected immediately via `validate_extension_matches_content`.

#### Security Boundaries & Limitations:
- **Header-Only Matching**: Signature checks identify file types strictly by their initial magic bytes. The upload layer does not parse internal file structures (such as PDF cross-reference tables or object streams, PNG chunk CRC checksums, TIFF IFD tag directories, or WebP bitstream compression).
- **No Antivirus or Malware Scanning**: The subsystem does not perform static analysis, binary disassembly, or antivirus inspection.
- **Polyglot & Embedded Exploit Limitations**: Files that embed valid magic-byte headers alongside malicious scripts, macros, or polyglot payloads will pass initial ingestion. Comprehensive structural parsing, text extraction, rasterization, and semantic sanitization are deferred to the Phase 4 OCR processing pipeline.

### 9.4 Bounded-Memory Streaming & Quota Enforcement
To prevent denial-of-service via memory exhaustion:
1. The file stream is consumed asynchronously using an async generator yielding chunks (default 1MB).
2. The initial chunk (up to 64KB) is read first to inspect magic bytes and detect content type.
3. If the initial chunk exceeds `MAX_UPLOAD_SIZE_BYTES`, the upload is aborted immediately with `UploadSizeExceededError` without invoking storage writes.
4. The async generator passes chunks directly to `storage.save(key, stream)`.
5. As chunks are read, cumulative byte count is tracked. If the total size exceeds `MAX_UPLOAD_SIZE_BYTES`, streaming halts immediately, partial staging files are unlinked by `LocalStorage`, and `UploadSizeExceededError` bubbles up to map consistently to `413 Payload Too Large`.

### 9.5 Transactional Consistency & Cancellation-Safe Compensation
A distributed state discrepancy can arise if file bytes are written to physical storage but the database transaction fails to commit (e.g., connection drop, timeout, constraint violation, or client cancellation).
- **Failure Compensation**: The upload service wraps database creation in a try/except block. If `session.commit()` fails, the database transaction is rolled back and `storage.delete(storage_key, missing_ok=True)` is immediately invoked to delete the newly written physical file.
- **Cancellation Safety (`asyncio.CancelledError`)**:
  - `asyncio.CancelledError` inherits from `BaseException` and is captured specifically.
  - If cancellation occurs *before* database commit is attempted, the transaction is rolled back and the storage object is unlinked.
  - If cancellation occurs *during* `session.commit()`, the service rolls back and queries PostgreSQL for `doc.id`. If the document was not persisted, the storage object is unlinked. If the document was committed prior to cancellation, the storage object is **preserved** to prevent creating a dangling database reference to a deleted file.
  - If the database state cannot be determined (e.g., connection drop during check), the storage object is preserved and a `CRITICAL` log is emitted for administrative reconciliation.
  - All cancellation cleanup is shielded (`asyncio.shield`) to prevent interruptions mid-deletion.
- **Detached Instance Safety**: Prior to database operations, the user's primary key (`user_id = user.id`) is copied to a local variable to prevent SQLAlchemy `DetachedInstanceError` when logging or handling errors post-rollback.
- **Deterministic Resource Cleanup**: The Starlette `UploadFile` handle is guaranteed to be closed in an outermost `finally` block across all successful, failed, and cancelled execution paths.

---

## 10. Document Listing, Search, Filtering & Metadata Management (Phase 3.4)

### 10.1 Architecture and Security Overview
Phase 3.4 implements authenticated discovery and metadata lookup endpoints:
- `GET /api/v1/documents`: Paginated, filtered, and deterministically sorted list of documents owned by the caller.
- `GET /api/v1/documents/{document_id}`: Metadata retrieval for a single document.

Both endpoints enforce strict zero-leakage security:
1. **Server-Derived Identity**: The owner ID is derived exclusively from `current_user.id`. The API never accepts a `user_id` parameter in request bodies, query strings, or client headers.
2. **Metadata-Only Operations**: Endpoints execute purely against PostgreSQL. Physical storage files are never opened, read, or checked, avoiding storage I/O overhead.
3. **Internal Key Suppression**: Opaque `storage_key` strings and physical filesystem paths are strictly excluded from response models (`DocumentResponse` and `PaginatedDocumentResponse`).

### 10.2 Strict Ownership Isolation & IDOR Defense
Multi-tenant isolation is enforced directly within the SQL query tree:
- In `list_documents`, the base condition `Document.user_id == user_id` is applied to both the total count query and the paginated item query.
- In `get_document_by_id`, the query matches `Document.id == document_id AND Document.user_id == user_id`.
- **Anti-Enumeration & IDOR Protection**: If a user requests a nonexistent document UUID or a document UUID belonging to another user, the API returns an identical `404 Not Found` response (`{"detail": "Document not found"}`). No distinction is made between nonexistent and foreign documents, preventing existence discovery attacks.

### 10.3 SQL-Level Pagination & Count Strategy
Pagination parameters are strictly validated at ingress:
- `limit`: Integer with constraints `1 <= limit <= 100`, defaulting to `20`.
- `offset`: Integer with constraint `offset >= 0`, defaulting to `0`.

Query Execution:
- The total count is calculated via `select(func.count()).select_from(Document).where(*conditions)`.
- If `total == 0`, the service short-circuits and immediately returns `([], 0)` without executing a second query.
- For non-zero totals, the item query applies `.limit(limit).offset(offset)` in PostgreSQL.
- Documents are never loaded into application RAM for Python-side slicing.

#### Pagination Consistency & Concurrency Limitations:
1. **Stable Ordering on Unchanged Datasets**: When the underlying dataset remains unchanged, primary column sorting combined with unique `Document.id` tie-breaking guarantees 100% deterministic, repeatable pagination without duplicates or missing items across pages.
2. **Concurrent Modifications Limitation**: The API operates under PostgreSQL `READ COMMITTED` isolation. As with all SQL `OFFSET` pagination architectures, if documents are concurrently inserted or deleted while a client paginates across separate HTTP requests, rows may shift across page boundaries. Transaction-spanning snapshots across distinct requests are not provided.

### 10.4 Deterministic Sorting & Tie-Breaking
Clients can sort by any of four supported model columns:
- `created_at` (default)
- `updated_at`
- `original_filename`
- `size_bytes`

In either direction (`asc` or `desc`, default `desc`).

To prevent unstable pagination (pagination jitter) when multiple documents share identical sort values (such as identical batch creation timestamps or identical file sizes), every query appends document UUID (`Document.id`) as a secondary tie-breaker:
- `desc` sort: `.order_by(primary_col.desc(), Document.id.desc())`
- `asc` sort: `.order_by(primary_col.asc(), Document.id.asc())`

Because `Document.id` is a unique primary key, ordering is guaranteed to be strictly deterministic for an unchanged dataset.

### 10.5 Metadata Filtering & Date Boundaries
- **Content Type (`content_type`)**: Exact string matching against the server-detected, stored `Document.content_type`. Searching for an unmatched format returns `{"items": [], "total": 0}` with HTTP 200.
- **Date Boundaries (`created_after`, `created_before`)**:
  - Accept ISO 8601 timezone-aware strings with an explicit UTC offset (e.g., `2026-11-01T00:00:00Z` or `2026-11-01T03:30:00+03:30`).
  - Require explicit timezone awareness (naive datetimes are rejected with `422 Unprocessable Content`).
  - Comparisons operate on absolute points in time normalized in PostgreSQL.
  - Semantics: `created_after` matches `Document.created_at >= created_after`; `created_before` matches `Document.created_at <= created_before`.
  - Chronological validation: If both are supplied, `created_after <= created_before` is enforced; inverted ranges return `422 Unprocessable Content`.

### 10.6 Parameterized Filename Substring Search (`q`)
Filename search operates over `Document.original_filename` using case-insensitive partial substring matching:
- **Wildcard Escaping**: SQL LIKE wildcards (`%` and `_`) and the escape character (`\`) are automatically escaped via `escape_like_wildcards()` before constructing the query: `Document.original_filename.ilike(f"%{escaped}%", escape="\\")`.
- **Literal Search**: Searching for `100%` or `report_2026` treats `%` and `_` as literal characters rather than arbitrary pattern wildcards.
- **Null Byte & Control Character Validation**: Search terms containing null bytes (`\0`) or ASCII control characters (`ord < 32` or `ord == 127`) are rejected with `422 Unprocessable Content` before querying the database, preventing database driver errors or unexpected behaviour.
- **SQL Injection Resistance**: Queries use SQLAlchemy parameterized parameter binding, preventing SQL injection.
- **Whitespace Handling**: Leading and trailing whitespace is trimmed. Whitespace-only search strings are treated as empty (no filter applied).
- **Input Length**: Search terms are limited to 255 characters (matching the maximum column length); longer inputs return `422 Unprocessable Content`.

### 10.7 Query Efficiency, Indexing & Scaling Considerations
- **Index Reuse**: Default listing and creation date filtering leverage the existing composite B-tree index `ix_documents_user_id_created_at` (`user_id`, `created_at`). Because `user_id` is the leading index column, owner filtering is index-accelerated without needing a redundant standalone index on `user_id`.
- **Scaling Filename Search**: The current substring search relies on PostgreSQL `ILIKE`. For datasets up to tens of thousands of documents per user, index scans on `user_id` followed by sequential filter evaluation on the user's subset of rows deliver sub-millisecond to low-millisecond latencies. For enterprise scale with millions of documents per user, adding the `pg_trgm` extension with a trigram GIN/GiST index on `(user_id, original_filename)` or delegating to an asynchronous search service is recommended in a later phase.

---

## 11. Secure Document Download & Reliable Coordinated Deletion (Phase 3.5)

Phase 3.5 completes the document lifecycle operations by implementing secure, authenticated streaming downloads and crash-resilient, coordinated deletion across PostgreSQL and physical storage.

### 11.1 Secure Streaming Download (`GET /api/v1/documents/{document_id}/download`)

#### Authorization and Access Sequence:
1. **Authentication**: Enforces JWT bearer token via `get_current_user`.
2. **Owner Verification Before Storage Access**: Queries PostgreSQL directly enforcing `Document.id == document_id` and `Document.user_id == current_user.id`.
3. **Lifecycle State Check**: Restricts access to active records (`Document.deletion_pending_at.is_(None)`). Documents marked pending deletion return HTTP 404 (indistinguishable from nonexistent or foreign-owned documents).
4. **Storage Access**: Calls `StorageBackend.open(storage_key)` only after successful authorization. Never exposes internal filesystem paths or storage keys to the client.

#### Streaming Architecture & Memory Bounds:
- Streams data in bounded chunks (`settings.STORAGE_CHUNK_SIZE`, default 1 MiB) via `StoredObjectReader.chunks()`.
- Never loads entire document contents into application RAM.
- Employs an async generator with a guaranteed `finally` block ensuring `reader.close()` executes on complete downloads, client disconnects, streaming errors, or request cancellation.

#### Standards-Compliant Headers:
- `Content-Type`: Server-verified media MIME type from database metadata.
- `Content-Length`: Verified byte size from database metadata describing the stored payload.
- `Content-Disposition`: Attachment format implementing RFC 6266 and RFC 5987. Constructs a safe ASCII fallback (`filename="document.pdf"`) alongside a percent-encoded UTF-8 attribute (`filename*=UTF-8''...`). Protects against CRLF injection, control characters, and path separators. Fully supports Persian (`فاکتور.pdf`), spaces, and parentheses.
- `X-Content-Type-Options: nosniff`: Prevents client MIME-type sniffing.
- `Cache-Control: private, no-cache`: Directs clients to revalidate cached responses.
- `ETag & Conditional Caching`: Exposes the verified SHA-256 digest in quoted format (`"hash"`). When an incoming `If-None-Match` header matches, returns HTTP 304 Not Modified immediately without opening storage.
- `Range Requests`: HTTP Range requests are not advertised (`Accept-Ranges` is omitted) and partial-content responses are not generated. Complete file streaming is enforced.

### 11.2 Reliable Coordinated Deletion (`DELETE /api/v1/documents/{document_id}`)

Because PostgreSQL transactions and filesystem deletions cannot participate in a single two-phase distributed commit, Phase 3.5 implements a durable, crash-resilient state machine:

```text
[ Active Document ]
        │
        ▼ (Step 1: SELECT ... FOR UPDATE)
[ Set deletion_pending_at = NOW() ]
        │
        ▼ (Step 2: Commit intent to PostgreSQL)
[ Deletion Intent Persisted ]
        │
        ▼ (Step 3: StorageBackend.delete(storage_key, missing_ok=True))
[ Physical File Deleted ]
        │
        ▼ (Step 4: DELETE FROM documents WHERE id = document_id)
[ Database Record Removed ] ──> HTTP 204 No Content
```

#### Durability & State Machine Semantics:
1. **Durable Intent Persistence**: The deletion intent is committed to PostgreSQL (`deletion_pending_at`) before attempting storage file removal.
2. **Metadata & Listing Masking**: Records with `deletion_pending_at IS NOT NULL` are immediately excluded from `GET /api/v1/documents`, detail queries (`GET /api/v1/documents/{id}`), and download endpoints (`GET /api/v1/documents/{id}/download`).
3. **Storage Unlink (`missing_ok=True`)**: Storage cleanup uses `missing_ok=True`. If an earlier attempt already deleted the file or the process crashed mid-way, subsequent retries proceed safely.
4. **Row Lock Concurrency Protection**: Step 1 acquires a row-level lock (`SELECT ... FOR UPDATE`), ensuring concurrent deletion requests for the same document execute deterministically without deadlocks or double-free errors.
5. **Database Finalization**: Metadata rows are removed only after physical storage deletion succeeds.
6. **Cancellation & Uncertain Commit Safety**: If database commits fail or are cancelled, the service queries row existence to verify whether the commit took effect or retains the pending-deletion state for retry reconciliation.
7. **Cross-Platform Handling**: On Windows, files held open by an active download stream prevent immediate unlinking (sharing violations). The storage deletion fails safely with `StoragePermissionError`, the database record remains in pending-deletion state, and a subsequent DELETE request finalizes cleanup once the reader closes. On POSIX platforms, unlinking an open inode safely dissociates the directory entry while the open file descriptor finishes streaming.

