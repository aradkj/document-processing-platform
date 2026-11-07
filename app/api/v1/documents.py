"""API endpoints for document persistence and secure uploads."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.db.models.user import User
from app.db.session import get_db_session
from app.schemas.document import DocumentResponse
from app.services.document import (
    DocumentPersistenceError,
    DocumentStorageError,
    UploadSizeExceededError,
    upload_document,
)
from app.services.document_validation import (
    EmptyFileError,
    ExtensionMismatchError,
    InvalidFilenameError,
    UnsupportedFileTypeError,
)
from app.storage import StorageBackend, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Documents"])


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a new document",
    description=(
        "Securely upload a document file (PDF, JPEG, PNG, TIFF, or WebP). "
        "Validates magic-byte signatures, calculates SHA-256 and byte size incrementally, "
        "enforces size limits via bounded streaming, publishes atomically to storage, "
        "and records metadata in PostgreSQL tied to the authenticated user."
    ),
    responses={
        status.HTTP_201_CREATED: {
            "description": "Document successfully uploaded and metadata persisted.",
            "model": DocumentResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Malformed request, missing filename, empty file, or invalid filename.",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated or invalid Bearer token.",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "File size exceeds the configured maximum upload limit.",
        },
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
            "description": "Unsupported media type, invalid file signature, or extension mismatch.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal server error during storage or persistence.",
        },
    },
)
async def upload_document_endpoint(
    file: Annotated[UploadFile, File(description="Document binary file to upload")],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[StorageBackend, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentResponse:
    """Handle multipart file upload, validate content, stream to storage, and persist metadata."""
    user_id_str = str(current_user.id)
    try:
        document = await upload_document(
            file=file,
            user=current_user,
            session=session,
            storage=storage,
            max_upload_size=settings.MAX_UPLOAD_SIZE_BYTES,
            chunk_size=settings.STORAGE_CHUNK_SIZE,
        )
        return DocumentResponse.model_validate(document)
    except (InvalidFilenameError, EmptyFileError) as exc:
        logger.warning(
            "Document upload rejected due to invalid request/file: %s",
            exc,
            extra={"user_id": user_id_str},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except UploadSizeExceededError as exc:
        logger.warning(
            "Document upload rejected: file size exceeded: %s",
            exc,
            extra={"user_id": user_id_str},
        )
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except (UnsupportedFileTypeError, ExtensionMismatchError) as exc:
        logger.warning(
            "Document upload rejected due to unsupported content or mismatch: %s",
            exc,
            extra={"user_id": user_id_str},
        )
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    except (DocumentStorageError, DocumentPersistenceError) as exc:
        logger.error(
            "Internal server error processing document upload: %s",
            exc,
            extra={"user_id": user_id_str},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process and store document upload.",
        ) from exc
