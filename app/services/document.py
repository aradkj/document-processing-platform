"""Document service handling secure streaming uploads and storage/database consistency."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

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
    - Deterministic file descriptor closing.
    """
    try:
        user_id = user.id

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
            logger.error("Failed persisting file to storage backend: %s", exc)
            raise DocumentStorageError("Failed to persist file in storage backend.") from exc

        # 6. Persist metadata record in PostgreSQL within transactional boundary
        doc = Document(
            id=uuid.uuid4(),
            user_id=user_id,
            storage_key=stored_obj.storage_key,
            original_filename=normalized_filename,
            content_type=detected_mime,
            size_bytes=stored_obj.size_bytes,
            sha256=stored_obj.checksum_sha256,
        )
        session.add(doc)

        try:
            await session.commit()
            await session.refresh(doc)
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

    finally:
        # Guarantee input file descriptor cleanup
        await file.close()
