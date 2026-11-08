"""API endpoints for document persistence, secure uploads, listing, and metadata retrieval."""

import logging
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.db.models.user import User
from app.db.session import get_db_session
from app.schemas.document import DocumentResponse, PaginatedDocumentResponse
from app.services.document import (
    DocumentPersistenceError,
    DocumentStorageError,
    UploadSizeExceededError,
    get_document_by_id,
    list_documents,
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


DocumentSortBy = Literal["created_at", "updated_at", "original_filename", "size_bytes"]
SortOrder = Literal["asc", "desc"]


@router.get(
    "",
    response_model=PaginatedDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="List authenticated user's documents",
    description=(
        "Retrieve a paginated, sorted, and filtered list of documents owned by the "
        "authenticated user. Strictly isolates records to the authenticated caller."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Paginated list of document metadata records.",
            "model": PaginatedDocumentResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated or invalid Bearer token.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Validation error in pagination, sorting, or date filtering parameters.",
        },
    },
)
async def list_documents_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Maximum number of document items to return (1-100).",
            examples=[20],
        ),
    ] = 20,
    offset: Annotated[
        int,
        Query(
            ge=0,
            description="Zero-based index offset for pagination.",
            examples=[0],
        ),
    ] = 0,
    sort_by: Annotated[
        DocumentSortBy,
        Query(
            description="Field to sort by (created_at, updated_at, original_filename, size_bytes).",
            examples=["created_at"],
        ),
    ] = "created_at",
    sort_order: Annotated[
        SortOrder,
        Query(
            description="Sort direction (asc for ascending, desc for descending).",
            examples=["desc"],
        ),
    ] = "desc",
    content_type: Annotated[
        str | None,
        Query(
            max_length=100,
            description="Filter by exact stored media MIME type (e.g. application/pdf, image/png).",
            examples=["application/pdf"],
        ),
    ] = None,
    created_after: Annotated[
        datetime | None,
        Query(
            description="Filter documents created at or after this ISO 8601 UTC timestamp.",
            examples=["2026-11-01T00:00:00Z"],
        ),
    ] = None,
    created_before: Annotated[
        datetime | None,
        Query(
            description="Filter documents created at or before this ISO 8601 UTC timestamp.",
            examples=["2026-11-30T23:59:59Z"],
        ),
    ] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=255,
            description=(
                "Search term for case-insensitive partial substring match on original_filename."
            ),
            examples=["invoice"],
        ),
    ] = None,
) -> PaginatedDocumentResponse:
    """List, search, filter, and paginate documents owned by the caller."""
    # Validate timezone-awareness
    if created_after is not None:
        if created_after.tzinfo is None or created_after.tzinfo.utcoffset(created_after) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Parameter 'created_after' must be a timezone-aware ISO 8601 datetime.",
            )
    if created_before is not None:
        if created_before.tzinfo is None or created_before.tzinfo.utcoffset(created_before) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Parameter 'created_before' must be a timezone-aware ISO 8601 datetime.",
            )

    # Validate date range consistency
    if created_after is not None and created_before is not None:
        if created_after > created_before:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Parameter 'created_after' cannot be later than 'created_before'.",
            )

    items, total = await list_documents(
        session=session,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        content_type=content_type,
        created_after=created_after,
        created_before=created_before,
        q=q,
    )

    return PaginatedDocumentResponse(
        items=[DocumentResponse.model_validate(doc) for doc in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Get document metadata by ID",
    description=(
        "Retrieve metadata for a specific document owned by the authenticated user. "
        "Returns 404 if the document does not exist or belongs to another user."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Document metadata retrieved successfully.",
            "model": DocumentResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated or invalid Bearer token.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Document not found.",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid document UUID format.",
        },
    },
)
async def get_document_endpoint(
    document_id: Annotated[
        uuid.UUID,
        Path(
            description="Unique logical identifier (UUID) of the document to retrieve.",
            examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
        ),
    ],
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentResponse:
    """Retrieve metadata for an individual document owned by the caller."""
    doc = await get_document_by_id(
        session,
        document_id=document_id,
        user_id=current_user.id,
    )
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    return DocumentResponse.model_validate(doc)


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
