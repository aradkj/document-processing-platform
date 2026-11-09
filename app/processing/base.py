"""Abstract base class and interface contract for document processors."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.processing.context import ProcessingContext
from app.processing.result import ProcessingResult
from app.processing.types import ProcessingCapability, ProcessorMetadata


class BaseProcessor(ABC):
    """Abstract interface defining document processor capabilities and execution contract.

    Processors are isolated execution units responsible for extracting metadata, text,
    or visual representations from document streams without coupling to HTTP layers,
    FastAPI requests, or database sessions.

    Storage and Seekability Note:
        The `context.reader` provides sequential streaming (`StoredObjectReader.chunks()`
        or `sync_chunks()`). It is NOT guaranteed to be seekable.
        Processors that require random access (e.g. PDF parsers or image decoders in Phase 4.3/4.4)
        must utilize a bounded temporary file adapter or memory buffer up to
        `context.limits.max_input_size_bytes` rather than assuming random-access streaming.
    """

    @property
    @abstractmethod
    def metadata(self) -> ProcessorMetadata:
        """Expose processor identity, semantic version, and supported formats."""
        ...

    @property
    def name(self) -> str:
        """Convenience property for processor name."""
        return self.metadata.name

    @property
    def version(self) -> str:
        """Convenience property for processor version."""
        return self.metadata.version

    @property
    def supported_formats(self) -> tuple[str, ...]:
        """Convenience property for supported MIME types."""
        return self.metadata.supported_formats

    @property
    def capabilities(self) -> tuple[ProcessingCapability, ...]:
        """Convenience property for declared capabilities."""
        return self.metadata.capabilities

    def supports_format(self, content_type: str) -> bool:
        """Determine whether this processor supports the specified media type."""
        normalized = content_type.strip().lower()
        return any(fmt.lower() == normalized for fmt in self.metadata.supported_formats)

    def supports_capability(self, capability: ProcessingCapability) -> bool:
        """Determine whether this processor declares support for a specific capability."""
        return capability in self.metadata.capabilities

    @abstractmethod
    async def process(self, context: ProcessingContext) -> ProcessingResult:
        """Execute processing against the supplied context.

        Args:
            context: Immutable processing context containing document metadata,
                     streaming reader, and resource boundaries.

        Returns:
            ProcessingResult containing structured metadata, text, and metrics.

        Raises:
            ProcessingError or subclasses on processing failure.
        """
        ...
