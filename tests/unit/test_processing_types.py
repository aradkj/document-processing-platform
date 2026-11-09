"""Unit tests for processing types, enums, metadata, and limits."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.processing.types import (
    DocumentFormat,
    ProcessingCapability,
    ProcessingLimits,
    ProcessingStatus,
    ProcessorMetadata,
)


def test_processing_status_enum_values() -> None:
    """Verify all processing status enum values."""
    assert ProcessingStatus.SUCCESS.value == "success"
    assert ProcessingStatus.PARTIAL_SUCCESS.value == "partial_success"
    assert ProcessingStatus.FAILED.value == "failed"


def test_processing_capability_enum_values() -> None:
    """Verify processing capability enum flags."""
    assert ProcessingCapability.METADATA_EXTRACTION.value == "metadata_extraction"
    assert ProcessingCapability.TEXT_EXTRACTION.value == "text_extraction"
    assert ProcessingCapability.PAGE_RENDERING.value == "page_rendering"
    assert ProcessingCapability.TABLE_EXTRACTION.value == "table_extraction"
    assert ProcessingCapability.IMAGE_ANALYSIS.value == "image_analysis"
    assert ProcessingCapability.OCR_READINESS.value == "ocr_readiness"


def test_document_format_enum_values() -> None:
    """Verify standard document format media types."""
    assert DocumentFormat.PDF.value == "application/pdf"
    assert DocumentFormat.JPEG.value == "image/jpeg"
    assert DocumentFormat.PNG.value == "image/png"
    assert DocumentFormat.TIFF.value == "image/tiff"
    assert DocumentFormat.WEBP.value == "image/webp"


def test_processor_metadata_valid() -> None:
    """Verify valid ProcessorMetadata instantiation."""
    meta = ProcessorMetadata(
        name="pdf-extractor",
        version="1.2.0",
        supported_formats=("application/pdf",),
        capabilities=(ProcessingCapability.TEXT_EXTRACTION,),
        description="Extracts text from PDF documents",
    )
    assert meta.name == "pdf-extractor"
    assert meta.version == "1.2.0"
    assert meta.supported_formats == ("application/pdf",)
    assert meta.capabilities == (ProcessingCapability.TEXT_EXTRACTION,)
    assert meta.description == "Extracts text from PDF documents"


@pytest.mark.parametrize("invalid_name", ["", "   ", "has spaces", "invalid@name", "path/name"])
def test_processor_metadata_invalid_name(invalid_name: str) -> None:
    """Verify rejection of invalid processor names."""
    with pytest.raises(ValueError, match="Processor name"):
        ProcessorMetadata(
            name=invalid_name,
            version="1.0.0",
            supported_formats=("application/pdf",),
        )


@pytest.mark.parametrize("invalid_version", ["", "   ", "v1.0", "1.0", "beta", "1.a.2"])
def test_processor_metadata_invalid_version(invalid_version: str) -> None:
    """Verify rejection of non-semantic version strings."""
    with pytest.raises(ValueError, match=r"Processor version cannot be empty|semantic versioning"):
        ProcessorMetadata(
            name="valid-processor",
            version=invalid_version,
            supported_formats=("application/pdf",),
        )


def test_processor_metadata_empty_formats() -> None:
    """Verify rejection of empty supported formats."""
    with pytest.raises(ValueError, match="at least one supported format"):
        ProcessorMetadata(
            name="valid-processor",
            version="1.0.0",
            supported_formats=(),
        )


@pytest.mark.parametrize("bad_format", ["", "pdf", "invalid_no_slash"])
def test_processor_metadata_invalid_format_string(bad_format: str) -> None:
    """Verify rejection of malformed MIME format strings."""
    with pytest.raises(ValueError, match="Expected valid MIME type"):
        ProcessorMetadata(
            name="valid-processor",
            version="1.0.0",
            supported_formats=(bad_format,),
        )


def test_processor_metadata_is_frozen() -> None:
    """Verify ProcessorMetadata is immutable."""
    meta = ProcessorMetadata(
        name="test-proc",
        version="1.0.0",
        supported_formats=("application/pdf",),
    )
    with pytest.raises(AttributeError):
        meta.name = "new-name"  # type: ignore[misc]


def test_processing_limits_defaults_and_validation() -> None:
    """Verify default processing limits and positive boundary validations."""
    limits = ProcessingLimits()
    assert limits.max_input_size_bytes == 20_971_520
    assert limits.timeout_seconds == 60.0
    assert limits.max_text_size_bytes == 10_485_760
    assert limits.max_pages == 1_000
    assert limits.max_image_pixels == 50_000_000
    assert limits.max_metadata_size_bytes == 65_536


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("max_input_size_bytes", 0),
        ("max_input_size_bytes", -1),
        ("timeout_seconds", 0.0),
        ("timeout_seconds", -5.0),
        ("max_text_size_bytes", 0),
        ("max_pages", 0),
        ("max_image_pixels", 0),
        ("max_metadata_size_bytes", 0),
    ],
)
def test_processing_limits_boundary_rejection(field: str, bad_value: int | float) -> None:
    """Verify strictly positive requirement on all resource limits."""
    kwargs: dict[str, Any] = {field: bad_value}
    with pytest.raises(ValueError, match="must be strictly positive"):
        ProcessingLimits(**kwargs)


def test_processing_limits_from_settings() -> None:
    """Verify factory construction of ProcessingLimits from Settings."""
    settings = Settings(
        PROCESSING_MAX_INPUT_SIZE_BYTES=15_000_000,
        PROCESSING_TIMEOUT_SECONDS=45.0,
        PROCESSING_MAX_TEXT_SIZE_BYTES=5_000_000,
        PROCESSING_MAX_PAGES=500,
        PROCESSING_MAX_IMAGE_PIXELS=25_000_000,
        PROCESSING_MAX_METADATA_SIZE_BYTES=32_768,
    )
    limits = ProcessingLimits.from_settings(settings)
    assert limits.max_input_size_bytes == 15_000_000
    assert limits.timeout_seconds == 45.0
    assert limits.max_text_size_bytes == 5_000_000
    assert limits.max_pages == 500
    assert limits.max_image_pixels == 25_000_000
    assert limits.max_metadata_size_bytes == 32_768
