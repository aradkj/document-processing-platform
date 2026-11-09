"""Unit tests for ProcessorRegistry deterministic registration and format lookup."""

from __future__ import annotations

import pytest

from app.processing.base import BaseProcessor
from app.processing.context import ProcessingContext
from app.processing.exceptions import (
    ProcessorRegistrationError,
    UnsupportedDocumentFormatError,
)
from app.processing.registry import (
    ProcessorRegistry,
    get_default_registry,
    reset_default_registry,
)
from app.processing.result import ProcessingResult
from app.processing.types import ProcessingCapability, ProcessorMetadata


class MockPdfProcessor(BaseProcessor):
    """Mock processor for PDF format."""

    @property
    def metadata(self) -> ProcessorMetadata:
        return ProcessorMetadata(
            name="mock-pdf-processor",
            version="1.0.0",
            supported_formats=("application/pdf",),
            capabilities=(ProcessingCapability.TEXT_EXTRACTION,),
        )

    async def process(self, context: ProcessingContext) -> ProcessingResult:
        return ProcessingResult(
            processor_name=self.name,
            processor_version=self.version,
            extracted_text="mock pdf content",
        )


class MockImageProcessor(BaseProcessor):
    """Mock processor for image formats (JPEG, PNG)."""

    @property
    def metadata(self) -> ProcessorMetadata:
        return ProcessorMetadata(
            name="mock-image-processor",
            version="1.0.0",
            supported_formats=("image/jpeg", "image/png"),
            capabilities=(ProcessingCapability.IMAGE_ANALYSIS,),
        )

    async def process(self, context: ProcessingContext) -> ProcessingResult:
        return ProcessingResult(
            processor_name=self.name,
            processor_version=self.version,
        )


def test_registry_registration_and_lookup() -> None:
    """Verify processor registration and lookup by name and format."""
    registry = ProcessorRegistry()
    pdf_proc = MockPdfProcessor()
    img_proc = MockImageProcessor()

    registry.register(pdf_proc)
    registry.register(img_proc)

    # Name lookup
    assert registry.get_processor("mock-pdf-processor") is pdf_proc
    assert registry.get_processor("mock-image-processor") is img_proc
    assert registry.has_processor("mock-pdf-processor")
    assert not registry.has_processor("unknown-proc")

    # Format resolution
    assert registry.get_processor_for_format("application/pdf") is pdf_proc
    assert registry.get_processor_for_format("image/jpeg") is img_proc
    assert registry.get_processor_for_format("IMAGE/PNG") is img_proc  # Case-insensitive
    assert registry.has_format("image/jpeg")
    assert not registry.has_format("application/zip")


def test_registry_duplicate_name_rejected() -> None:
    """Verify registering a processor with an existing name raises error."""
    registry = ProcessorRegistry()
    registry.register(MockPdfProcessor())

    with pytest.raises(ProcessorRegistrationError, match="already registered"):
        registry.register(MockPdfProcessor())


def test_registry_format_collision_rejected() -> None:
    """Verify another processor cannot register an already mapped format without allow_replace."""
    registry = ProcessorRegistry()
    registry.register(MockPdfProcessor())

    class CollidingProcessor(BaseProcessor):
        @property
        def metadata(self) -> ProcessorMetadata:
            return ProcessorMetadata(
                name="colliding-processor",
                version="2.0.0",
                supported_formats=("application/pdf",),
            )

        async def process(self, context: ProcessingContext) -> ProcessingResult:
            return ProcessingResult(processor_name=self.name, processor_version=self.version)

    with pytest.raises(
        ProcessorRegistrationError, match="Format 'application/pdf' is already registered"
    ):
        registry.register(CollidingProcessor())


def test_registry_allow_replace() -> None:
    """Verify allow_replace permits overriding existing processor and format mappings."""
    registry = ProcessorRegistry()
    pdf_v1 = MockPdfProcessor()
    registry.register(pdf_v1)

    class ReplacementPdfProcessor(BaseProcessor):
        @property
        def metadata(self) -> ProcessorMetadata:
            return ProcessorMetadata(
                name="mock-pdf-processor",
                version="2.0.0",
                supported_formats=("application/pdf",),
            )

        async def process(self, context: ProcessingContext) -> ProcessingResult:
            return ProcessingResult(processor_name=self.name, processor_version=self.version)

    pdf_v2 = ReplacementPdfProcessor()
    registry.register(pdf_v2, allow_replace=True)

    assert registry.get_processor("mock-pdf-processor") is pdf_v2
    assert registry.get_processor_for_format("application/pdf") is pdf_v2


def test_registry_unsupported_format_raises() -> None:
    """Verify resolving an unsupported format raises UnsupportedDocumentFormatError."""
    registry = ProcessorRegistry()
    registry.register(MockPdfProcessor())

    with pytest.raises(
        UnsupportedDocumentFormatError, match="No processor is registered for format"
    ):
        registry.get_processor_for_format("video/mp4")


def test_registry_unregistered_name_raises() -> None:
    """Verify retrieving an unregistered processor name raises ProcessorRegistrationError."""
    registry = ProcessorRegistry()
    with pytest.raises(ProcessorRegistrationError, match="is not registered"):
        registry.get_processor("non-existent")


def test_registry_listing_and_sorting() -> None:
    """Verify deterministic alphabetical sorting of processors and formats."""
    registry = ProcessorRegistry()
    registry.register(MockImageProcessor())
    registry.register(MockPdfProcessor())

    procs = registry.list_processors()
    assert len(procs) == 2
    assert procs[0].name == "mock-image-processor"
    assert procs[1].name == "mock-pdf-processor"

    fmts = registry.supported_formats()
    assert fmts == ["application/pdf", "image/jpeg", "image/png"]


def test_registry_instance_isolation() -> None:
    """Verify separate ProcessorRegistry instances do not leak state."""
    r1 = ProcessorRegistry()
    r2 = ProcessorRegistry()

    r1.register(MockPdfProcessor())
    assert r1.has_processor("mock-pdf-processor")
    assert not r2.has_processor("mock-pdf-processor")


def test_default_registry_lifecycle() -> None:
    """Verify get_default_registry and reset_default_registry behavior."""
    try:
        reset_default_registry()
        reg1 = get_default_registry()
        reg2 = get_default_registry()
        assert reg1 is reg2

        reg1.register(MockPdfProcessor())
        assert reg2.has_processor("mock-pdf-processor")

        reset_default_registry()
        reg3 = get_default_registry()
        assert not reg3.has_processor("mock-pdf-processor")
    finally:
        reset_default_registry()
