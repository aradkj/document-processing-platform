"""Document service handling secure streaming uploads and storage/database consistency."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
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
) -> Document | None:
    """Retrieve metadata for a single document owned by user_id.

    Returns None if the document does not exist or belongs to another user.
    """
    stmt = select(Document).where(
        Document.id == document_id,
        Document.user_id == user_id,
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

    Guarantees:
    - Strict owner isolation in SQL (Document.user_id == user_id).
    - Database-level pagination via LIMIT and OFFSET.
    - Deterministic tie-breaking using Document.id.
    - Parameterized SQL with wildcard escaping for filename search.
    - Total count calculated using identical filtering criteria.
    """
    conditions = [Document.user_id == user_id]

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
