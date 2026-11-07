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
2. **ORM Configuration**: `User.documents` specifies `passive_deletes=True` with no `cascade="all, delete-orphan"` or delete cascades.
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

## 8. Scope Boundaries & Deferred Features

The following items are intentionally **out of scope** for Phase 3.2 and deferred to Phase 3.3 and later:
- Document upload, download, and delete HTTP endpoints (`POST /documents`, `GET /documents/{id}`).
- MIME type sniffing, magic byte validation, and upload stream parsing (Phase 3.3).
- Document processing pipeline, OCR execution, and text extraction (Phase 4+).
- Background task queues (Celery/RabbitMQ/Redis).
- Document tags, folders, sharing, and fine-grained permissions.

