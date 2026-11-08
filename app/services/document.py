"""Document service handling secure streaming uploads and storage/database consistency."""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.db.models.document import Document
from app.db.models.user import User
from app.services.document_validation import (
    EmptyFileError,
    detect_content_type,
    validate_and_sanitize_filename,
    validate_extension_matches_content,
)
from app.storage.base import StorageBackend
from app.storage.exceptions import StorageError
from app.storage.types import StoredObject

logger = logging.getLogger(__name__)


class DocumentUploadError(Exception):
    """Base exception for document upload operations."""


class UploadSizeExceededError(DocumentUploadError):
    """Raised when an uploaded file exceeds the configured maximum upload size."""


class DocumentStorageError(DocumentUploadError):
    """Raised when an unexpected error occurs during file storage persistence."""


class DocumentPersistenceError(DocumentUploadError):
    """Raised when document metadata persistence in PostgreSQL fails."""


class DocumentDeletionError(Exception):
    """Base exception for document deletion failures."""


class DocumentNotFoundError(DocumentDeletionError):
    """Raised when document does not exist or does not belong to the user."""


class DocumentStorageDeletionError(DocumentDeletionError):
    """Raised when physical storage deletion fails, retaining document in pending state."""


class DocumentDatabaseFinalizationError(DocumentDeletionError):
    """Raised when database row removal fails after physical deletion."""


async def upload_document(
    *,
    file: UploadFile,
    user: User,
    session: AsyncSession,
    storage: StorageBackend,
    max_upload_size: int,
    chunk_size: int,
) -> Document:
    """Validate, stream to storage, and persist a document in PostgreSQL.

    Guarantees:
    - Bounded-memory streaming (never loads complete file into RAM).
    - Immediate size enforcement during streaming.
    - Opaque storage keys generated via StorageBackend.
    - Owner identity derived strictly from authenticated user.
    - Atomic storage compensation (deleting stored file if database commit fails).
    - Cancellation-safe compensation preventing orphaned storage files.
    - Deterministic file descriptor closing.
    """
    stored_obj: StoredObject | None = None
    commit_attempted = False
    committed = False
    doc_id: uuid.UUID | None = None
    user_id = user.id

    try:
        # 1. Validate and normalize user-provided original filename
        normalized_filename = validate_and_sanitize_filename(file.filename)

        # 2. Read initial inspection buffer for magic byte signature detection
        # Use 64KB or chunk_size, whichever is smaller, to preserve bounded memory
        initial_buffer_size = min(chunk_size, 65536)
        header_chunk = await file.read(initial_buffer_size)

        if not header_chunk:
            raise EmptyFileError("Uploaded file is empty.")

        if len(header_chunk) > max_upload_size:
            raise UploadSizeExceededError(
                f"File size exceeds maximum allowed upload size of {max_upload_size} bytes."
            )

        # 3. Detect media type via content signature and verify extension match
        detected_mime = detect_content_type(header_chunk)
        validate_extension_matches_content(normalized_filename, detected_mime)

        # 4. Construct bounded async generator for streaming to storage backend
        total_bytes = len(header_chunk)

        async def upload_stream() -> AsyncIterator[bytes]:
            nonlocal total_bytes
            # Yield initial inspection buffer exactly once
            yield header_chunk

            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_upload_size:
                    raise UploadSizeExceededError(
                        f"File size exceeds maximum allowed upload size of {max_upload_size} bytes."
                    )
                yield chunk

        # 5. Persist to storage backend with generated opaque key
        storage_key = storage.generate_key()
        try:
            stored_obj = await storage.save(upload_stream(), storage_key=storage_key)
        except UploadSizeExceededError:
            # Re-raise size limit exceptions directly
            raise
        except StorageError as exc:
            # Check if this was caused by an UploadSizeExceededError from the stream
            if isinstance(exc.__cause__, UploadSizeExceededError):
                raise exc.__cause__ from None
            logger.error("Failed persisting file to storage backend: %s", exc)
            raise DocumentStorageError("Failed to persist file in storage backend.") from exc

        # 6. Persist metadata record in PostgreSQL within transactional boundary
        doc_id = uuid.uuid4()
        doc = Document(
            id=doc_id,
            user_id=user_id,
            storage_key=stored_obj.storage_key,
            original_filename=normalized_filename,
            content_type=detected_mime,
            size_bytes=stored_obj.size_bytes,
            sha256=stored_obj.checksum_sha256,
        )
        session.add(doc)

        try:
            commit_attempted = True
            await session.commit()
            committed = True
            await session.refresh(doc)
        except asyncio.CancelledError:
            # Cancellation during or after commit will be handled by outer cancellation block
            raise
        except Exception as db_exc:
            # 7. Database failure compensation: unlink stored physical file
            await session.rollback()
            logger.error(
                "Database persistence failed after storing file. Initiating compensation cleanup.",
                extra={
                    "storage_key": stored_obj.storage_key,
                    "user_id": str(user_id),
                    "error": str(db_exc),
                },
            )
            try:
                await storage.delete(stored_obj.storage_key, missing_ok=True)
                logger.info(
                    "Storage compensation succeeded: deleted orphaned storage object '%s'.",
                    stored_obj.storage_key,
                )
            except Exception as cleanup_exc:
                logger.critical(
                    "CRITICAL: Storage compensation FAILED! Orphaned file with key '%s'. "
                    "Requires reconciliation. Error: %s",
                    stored_obj.storage_key,
                    cleanup_exc,
                )
            raise DocumentPersistenceError("Failed to persist document metadata.") from db_exc

        logger.info(
            "Document successfully uploaded and persisted",
            extra={
                "document_id": str(doc.id),
                "user_id": str(user_id),
                "size_bytes": doc.size_bytes,
                "content_type": doc.content_type,
            },
        )
        return doc

    except asyncio.CancelledError as cancel_exc:
        # Task cancellation occurred
        if stored_obj is not None and not committed:
            # A storage object was published, but commit is not known to have succeeded
            should_delete = False
            if not commit_attempted:
                # Commit was never called: transaction definitely did not commit
                should_delete = True
                try:
                    await session.rollback()
                except Exception:
                    pass
            else:
                # Commit was attempted and cancelled. Check if PostgreSQL committed the row.
                try:
                    await session.rollback()
                except Exception:
                    pass

                try:
                    res = await session.execute(select(Document.id).where(Document.id == doc_id))
                    persisted = res.scalar_one_or_none() is not None
                    if persisted:
                        logger.warning(
                            "Cancellation during commit, but document record '%s' was committed. "
                            "Preserving storage object '%s'.",
                            doc_id,
                            stored_obj.storage_key,
                            extra={"user_id": str(user_id), "storage_key": stored_obj.storage_key},
                        )
                        should_delete = False
                    else:
                        should_delete = True
                except Exception as check_exc:
                    # Outcome uncertain: DO NOT delete storage to avoid corrupting DB doc
                    logger.critical(
                        "CRITICAL: Cancellation during commit with uncertain database outcome. "
                        "Storage key '%s' preserved for reconciliation. Database check error: %s",
                        stored_obj.storage_key,
                        check_exc,
                        extra={"user_id": str(user_id), "storage_key": stored_obj.storage_key},
                    )
                    should_delete = False

            if should_delete:
                target_key = stored_obj.storage_key

                async def _perform_cancellation_cleanup() -> None:
                    try:
                        await storage.delete(target_key, missing_ok=True)
                        logger.info(
                            "Cancellation cleanup succeeded: deleted orphaned storage object '%s'.",
                            target_key,
                            extra={"user_id": str(user_id), "storage_key": target_key},
                        )
                    except Exception as cleanup_exc:
                        logger.critical(
                            "CRITICAL: Cancellation storage cleanup FAILED! Orphaned key '%s'. "
                            "Requires reconciliation. Error: %s",
                            target_key,
                            cleanup_exc,
                            extra={"user_id": str(user_id), "storage_key": target_key},
                        )

                cleanup_task = asyncio.create_task(_perform_cancellation_cleanup())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task

        raise cancel_exc

    finally:
        # Guarantee input file descriptor cleanup
        await file.close()


def format_content_disposition(filename: str) -> str:
    """Format an RFC 6266 and RFC 5987 compliant Content-Disposition header value.

    Guarantees:
    - Protects against CRLF injection and header manipulation.
    - Strips path separators and control characters.
    - Provides a safe ASCII fallback quoted-string for legacy clients.
    - Provides a UTF-8 percent-encoded filename* parameter for modern clients.
    - Properly supports Persian and other Unicode scripts, spaces, and parentheses.
    """
    sanitized = re.sub(r"[\r\n\x00-\x1f\x7f/\\]", "", filename).strip()
    if not sanitized:
        sanitized = "document"

    stem = ""
    ext = ""
    if "." in sanitized:
        stem, ext = sanitized.rsplit(".", 1)
        ext = "." + ext
    else:
        stem = sanitized

    ascii_stem = "".join(
        ch if (32 <= ord(ch) <= 126 and ch not in ('"', "\\", ";")) else "_" for ch in stem
    ).strip(" ._")

    ascii_ext = "".join(
        ch if (32 <= ord(ch) <= 126 and ch not in ('"', "\\", ";")) else "" for ch in ext
    )

    if not ascii_stem:
        ascii_stem = "document"

    ascii_clean = f"{ascii_stem}{ascii_ext}"
    utf8_encoded = urllib.parse.quote(sanitized, safe="!#$&+-.^_`|~")
    return f"attachment; filename=\"{ascii_clean}\"; filename*=UTF-8''{utf8_encoded}"


ETAG_LIST_PATTERN = re.compile(r'(?:(W/))?"([^"]*)"', re.ASCII)


def matches_if_none_match(header_val: str | None, current_etag: str) -> bool:
    """Evaluate RFC 9110 weak comparison semantics for HTTP If-None-Match on GET.

    Handles:
    - Exact matching strong ETag: '"hash"'
    - Wildcard: '*'
    - Multiple comma-separated ETags: '"tag1", "hash", "tag2"'
    - ETags containing commas inside quotes: '"tag,with,comma", "hash"'
    - Weak ETags: 'W/"hash"'
    - Malformed input safely returns False (stream full content).
    """
    if not header_val:
        return False

    cleaned = header_val.strip()
    if not cleaned:
        return False

    # Extract target opaque-tag
    target_opaque = current_etag
    if target_opaque.startswith("W/"):
        target_opaque = target_opaque[2:]
    if target_opaque.startswith('"') and target_opaque.endswith('"') and len(target_opaque) >= 2:
        target_opaque = target_opaque[1:-1]

    # RFC 9110 Section 13.1.2: '*' matches any existing representation
    if cleaned == "*":
        return True

    try:
        for match in ETAG_LIST_PATTERN.finditer(cleaned):
            _weak_prefix, tag_value = match.groups()
            if tag_value == target_opaque:
                return True
    except Exception:
        return False

    return False


def escape_like_wildcards(text: str, escape_char: str = "\\") -> str:
    """Escape SQL LIKE wildcard characters (%, _) and the escape character itself."""
    return (
        text.replace(escape_char, escape_char * 2)
        .replace("%", f"{escape_char}%")
        .replace("_", f"{escape_char}_")
    )


async def get_document_by_id(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    include_pending_deletion: bool = False,
) -> Document | None:
    """Retrieve metadata for a single document owned by user_id.

    Returns None if the document does not exist, belongs to another user,
    or is marked pending deletion (unless include_pending_deletion is True).
    """
    conditions = [
        Document.id == document_id,
        Document.user_id == user_id,
    ]
    if not include_pending_deletion:
        conditions.append(Document.deletion_pending_at.is_(None))
    stmt = select(Document).where(*conditions)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_document_for_download(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Document | None:
    """Retrieve an active, non-pending document for authenticated streaming download.

    Enforces ownership and active lifecycle state directly in PostgreSQL.
    Returns None if nonexistent, foreign-owned, or pending deletion.
    """
    stmt = select(Document).where(
        Document.id == document_id,
        Document.user_id == user_id,
        Document.deletion_pending_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_documents(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    content_type: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    q: str | None = None,
) -> tuple[list[Document], int]:
    """Query, filter, and paginate documents owned by user_id with deterministic sorting.

    Guarantees & Semantics:
    - Strict owner isolation in SQL (Document.user_id == user_id).
    - Hides pending-deletion documents (Document.deletion_pending_at.is_(None)).
    - Database-level pagination via LIMIT and OFFSET.
    - Deterministic tie-breaking using Document.id: ensures stable ordering across
      paginated pages when the underlying dataset remains unchanged.
    - Concurrency Limitation: Under standard SQL LIMIT/OFFSET pagination in READ COMMITTED
      isolation, concurrent inserts or deletions by the user between separate HTTP requests
      may cause items to shift across page boundaries.
    - Parameterized SQL with wildcard escaping for filename search.
    - Total count calculated using identical filtering criteria.
    """
    conditions = [
        Document.user_id == user_id,
        Document.deletion_pending_at.is_(None),
    ]

    if content_type is not None:
        trimmed_ct = content_type.strip()
        if trimmed_ct:
            conditions.append(Document.content_type == trimmed_ct)

    if created_after is not None:
        conditions.append(Document.created_at >= created_after)

    if created_before is not None:
        conditions.append(Document.created_at <= created_before)

    if q is not None:
        trimmed_q = q.strip()
        if trimmed_q:
            escaped_q = escape_like_wildcards(trimmed_q)
            conditions.append(Document.original_filename.ilike(f"%{escaped_q}%", escape="\\"))

    count_stmt = select(func.count()).select_from(Document).where(*conditions)
    count_res = await session.execute(count_stmt)
    total = count_res.scalar_one()

    if total == 0:
        return [], 0

    sort_col: InstrumentedAttribute[Any]
    if sort_by == "updated_at":
        sort_col = Document.updated_at
    elif sort_by == "original_filename":
        sort_col = Document.original_filename
    elif sort_by == "size_bytes":
        sort_col = Document.size_bytes
    else:
        sort_col = Document.created_at

    if sort_order == "asc":
        query_stmt = (
            select(Document)
            .where(*conditions)
            .order_by(sort_col.asc(), Document.id.asc())
            .limit(limit)
            .offset(offset)
        )
    else:
        query_stmt = (
            select(Document)
            .where(*conditions)
            .order_by(sort_col.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
        )
    result = await session.execute(query_stmt)
    items = list(result.scalars().all())

    return items, total


async def delete_document(
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession,
    storage: StorageBackend,
) -> bool:
    """Reliably and durably coordinate document deletion across PostgreSQL and StorageBackend.

    Lifecycle Steps:
    1. Authorization & Lookup:
       Query document with row-level lock (FOR UPDATE) scoped strictly to (document_id, user_id).
       If not found, raise DocumentNotFoundError (caller maps to 404).
    2. Durable Deletion Intent:
       If document is active (deletion_pending_at is None):
           Mark document.deletion_pending_at = datetime.now(UTC).
           Commit deletion intent to PostgreSQL immediately.
           If commit fails: rollback, document remains active, file is untouched.
       If document is already pending deletion:
           Intent is already persisted; resume deletion workflow.
    3. Storage Cleanup:
       Call storage.delete(storage_key, missing_ok=True).
       If storage.delete fails:
           Keep document in pending-deletion state in PostgreSQL.
           Do not remove database row.
           Raise DocumentStorageDeletionError (caller returns 500/503 for retry).
    4. Database Finalization:
       Delete document row from PostgreSQL and commit.
       If commit fails or is cancelled:
           Re-verify database state to ensure row deletion or retain pending state.
           Raise DocumentDatabaseFinalizationError if row could not be confirmed deleted.
    5. Success:
       Returns True when both physical object and database record are confirmed removed.
    """
    # Step 1: Authorization and lookup with row lock
    stmt = (
        select(Document)
        .where(Document.id == document_id, Document.user_id == user_id)
        .with_for_update()
    )
    res = await session.execute(stmt)
    doc = res.scalar_one_or_none()

    if doc is None:
        raise DocumentNotFoundError("Document not found.")

    storage_key = doc.storage_key

    # Step 2: Persist deletion intent if not already pending
    if doc.deletion_pending_at is None:
        doc.deletion_pending_at = datetime.now(UTC)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(
                "Failed to persist deletion intent for document '%s': %s",
                document_id,
                exc,
                extra={"document_id": str(document_id), "user_id": str(user_id)},
            )
            raise DocumentDeletionError("Failed to persist document deletion intent.") from exc
    else:
        # Commit/release row lock during storage deletion
        await session.commit()

    # Step 3: Physical storage deletion
    try:
        await storage.delete(storage_key, missing_ok=True)
    except Exception as exc:
        logger.error(
            "Storage deletion failed for doc '%s' (key='%s'). Retaining pending state: %s",
            document_id,
            storage_key,
            exc,
            extra={"document_id": str(document_id), "user_id": str(user_id)},
        )
        raise DocumentStorageDeletionError("Failed to remove stored physical object.") from exc

    # Step 4: Finalize database row removal
    try:
        res = await session.execute(
            select(Document)
            .where(Document.id == document_id, Document.user_id == user_id)
            .with_for_update()
        )
        doc_to_delete = res.scalar_one_or_none()
        if doc_to_delete is not None:
            await session.delete(doc_to_delete)
            await session.commit()
        else:
            await session.commit()
    except asyncio.CancelledError as cancel_exc:
        try:
            await session.rollback()
        except Exception:
            pass
        try:
            check_res = await session.execute(select(Document.id).where(Document.id == document_id))
            if check_res.scalar_one_or_none() is not None:
                logger.warning(
                    "Deletion cancelled during final commit; doc '%s' remains in pending state.",
                    document_id,
                    extra={"document_id": str(document_id), "user_id": str(user_id)},
                )
            else:
                logger.info(
                    "Deletion cancelled during commit, but row '%s' was deleted successfully.",
                    document_id,
                    extra={"document_id": str(document_id), "user_id": str(user_id)},
                )
        except Exception:
            pass
        raise cancel_exc
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        try:
            check_res = await session.execute(select(Document.id).where(Document.id == document_id))
            if check_res.scalar_one_or_none() is None:
                return True
        except Exception:
            pass
        logger.critical(
            "CRITICAL: Failed to finalize database deletion for doc '%s' after file removed. "
            "Document remains in pending state for retry. Error: %s",
            document_id,
            exc,
            extra={"document_id": str(document_id), "user_id": str(user_id)},
        )
        raise DocumentDatabaseFinalizationError("Failed to finalize document removal.") from exc

    return True
