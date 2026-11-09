"""Processing subsystem data types, enums, metadata, and limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings


class ProcessingStatus(StrEnum):
    """Execution status of a document processing operation."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ProcessingCapability(StrEnum):
    """Capabilities supported and exposed by a document processor."""

    METADATA_EXTRACTION = "metadata_extraction"
    TEXT_EXTRACTION = "text_extraction"
    PAGE_RENDERING = "page_rendering"
    TABLE_EXTRACTION = "table_extraction"
    IMAGE_ANALYSIS = "image_analysis"
    OCR_READINESS = "ocr_readiness"


class DocumentFormat(StrEnum):
    """Standardized media type identifiers for supported document formats."""

    PDF = "application/pdf"
    JPEG = "image/jpeg"
    PNG = "image/png"
    TIFF = "image/tiff"
    WEBP = "image/webp"


_PROCESSOR_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?$")


@dataclass(frozen=True, slots=True)
class ProcessorMetadata:
    """Metadata describing a processor's identity, version, and capabilities."""

    name: str
    version: str
    supported_formats: tuple[str, ...]
    capabilities: tuple[ProcessingCapability, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        """Validate processor metadata fields."""
        if not self.name or not self.name.strip():
            raise ValueError("Processor name cannot be empty.")
        if not _PROCESSOR_NAME_PATTERN.match(self.name.strip()):
            raise ValueError(
                f"Processor name '{self.name}' must contain only alphanumeric characters, "
                "dashes, or underscores."
            )

        if not self.version or not self.version.strip():
            raise ValueError("Processor version cannot be empty.")
        if not _VERSION_PATTERN.match(self.version.strip()):
            raise ValueError(
                f"Processor version '{self.version}' must follow "
                "semantic versioning (e.g. '1.0.0')."
            )

        if not self.supported_formats:
            raise ValueError("Processor must declare at least one supported format.")

        for fmt in self.supported_formats:
            if not fmt or "/" not in fmt:
                raise ValueError(f"Invalid format declaration '{fmt}'. Expected valid MIME type.")


@dataclass(frozen=True, slots=True)
class ProcessingLimits:
    """Resource and execution boundaries enforced during document processing."""

    max_input_size_bytes: int = 20_971_520  # 20 MiB
    timeout_seconds: float = 60.0
    max_text_size_bytes: int = 10_485_760  # 10 MiB
    max_pages: int = 1_000
    max_image_pixels: int = 50_000_000  # 50 MP
    max_metadata_size_bytes: int = 65_536  # 64 KiB

    def __post_init__(self) -> None:
        """Enforce positive and valid boundary constraints for processing limits."""
        if self.max_input_size_bytes <= 0:
            raise ValueError("max_input_size_bytes must be strictly positive.")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be strictly positive.")
        if self.max_text_size_bytes <= 0:
            raise ValueError("max_text_size_bytes must be strictly positive.")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be strictly positive.")
        if self.max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be strictly positive.")
        if self.max_metadata_size_bytes <= 0:
            raise ValueError("max_metadata_size_bytes must be strictly positive.")

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ProcessingLimits:
        """Construct ProcessingLimits from application settings."""
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()

        return cls(
            max_input_size_bytes=settings.PROCESSING_MAX_INPUT_SIZE_BYTES,
            timeout_seconds=settings.PROCESSING_TIMEOUT_SECONDS,
            max_text_size_bytes=settings.PROCESSING_MAX_TEXT_SIZE_BYTES,
            max_pages=settings.PROCESSING_MAX_PAGES,
            max_image_pixels=settings.PROCESSING_MAX_IMAGE_PIXELS,
            max_metadata_size_bytes=settings.PROCESSING_MAX_METADATA_SIZE_BYTES,
        )
