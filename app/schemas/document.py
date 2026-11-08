"""Pydantic schemas for document entities and upload responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentResponse(BaseModel):
    """Structured response payload representing persisted document metadata."""

    id: uuid.UUID = Field(
        ...,
        description="Unique logical identifier for the document",
        examples=["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
    )
    original_filename: str = Field(
        ...,
        description="Original user-provided filename, normalized and validated for display",
        examples=["quarterly_report_2026.pdf"],
    )
    content_type: str = Field(
        ...,
        description="Detected media MIME type based on actual file content signature",
        examples=["application/pdf"],
    )
    size_bytes: int = Field(
        ...,
        description="Actual stored binary payload size in bytes",
        examples=[1048576],
    )
    sha256: str = Field(
        ...,
        description="Hexadecimal SHA-256 digest calculated from actual uploaded bytes",
        examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    )
    created_at: datetime = Field(
        ...,
        description="UTC creation timestamp when document was persisted",
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp of last document modification",
    )

    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
    )


class PaginatedDocumentResponse(BaseModel):
    """Paginated list of document metadata records."""

    items: list[DocumentResponse] = Field(
        ...,
        description="List of document metadata items matching query criteria",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of documents matching filters before pagination",
        examples=[42],
    )
    limit: int = Field(
        ...,
        ge=1,
        le=100,
        description="Maximum number of items returned in this page",
        examples=[20],
    )
    offset: int = Field(
        ...,
        ge=0,
        description="Zero-based index offset for pagination",
        examples=[0],
    )

    model_config = ConfigDict(
        extra="forbid",
    )
