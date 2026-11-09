"""Processor registry providing deterministic processor registration and format resolution."""

from __future__ import annotations

from app.processing.base import BaseProcessor
from app.processing.exceptions import (
    ProcessorRegistrationError,
    UnsupportedDocumentFormatError,
)


class ProcessorRegistry:
    """Registry maintaining registered document processors and format mappings.

    Ensures deterministic format resolution without silent fallbacks, duplicate registration
    conflicts, or tight coupling to file parsing or storage layers.
    """

    def __init__(self) -> None:
        self._processors: dict[str, BaseProcessor] = {}
        self._format_map: dict[str, BaseProcessor] = {}

    def register(self, processor: BaseProcessor, *, allow_replace: bool = False) -> None:
        """Register a processor instance.

        Args:
            processor: Concrete processor implementing BaseProcessor.
            allow_replace: Whether to allow overriding an existing processor with the
                same name or format.

        Raises:
            ProcessorRegistrationError: If name or format conflicts exist and
                allow_replace is False.
        """
        name = processor.name
        if not allow_replace and name in self._processors:
            raise ProcessorRegistrationError(
                f"Processor '{name}' is already registered. Duplicate registrations are rejected."
            )

        # Check for format collisions
        for raw_fmt in processor.supported_formats:
            fmt = raw_fmt.strip().lower()
            existing = self._format_map.get(fmt)
            if existing is not None and existing.name != name and not allow_replace:
                raise ProcessorRegistrationError(
                    f"Format '{fmt}' is already registered to processor '{existing.name}'."
                )

        # Apply registrations
        self._processors[name] = processor
        for raw_fmt in processor.supported_formats:
            fmt = raw_fmt.strip().lower()
            self._format_map[fmt] = processor

    def get_processor(self, name: str) -> BaseProcessor:
        """Retrieve a registered processor by its name.

        Args:
            name: Stable processor identifier.

        Returns:
            The registered BaseProcessor instance.

        Raises:
            ProcessorRegistrationError: If processor name is not found.
        """
        processor = self._processors.get(name)
        if processor is None:
            raise ProcessorRegistrationError(f"Processor '{name}' is not registered.")
        return processor

    def get_processor_for_format(self, content_type: str) -> BaseProcessor:
        """Resolve the appropriate processor for a normalized media type.

        Args:
            content_type: MIME type string (e.g. 'application/pdf', 'image/jpeg').

        Returns:
            The registered BaseProcessor instance for this format.

        Raises:
            UnsupportedDocumentFormatError: If no processor supports the specified format.
        """
        normalized = content_type.strip().lower()
        processor = self._format_map.get(normalized)
        if processor is None:
            raise UnsupportedDocumentFormatError(
                f"No processor is registered for format '{content_type}'."
            )
        return processor

    def has_processor(self, name: str) -> bool:
        """Check if a processor with the given name is registered."""
        return name in self._processors

    def has_format(self, content_type: str) -> bool:
        """Check if a processor is registered for the specified format."""
        return content_type.strip().lower() in self._format_map

    def list_processors(self) -> list[BaseProcessor]:
        """Return all registered processors sorted deterministically by name."""
        return [self._processors[k] for k in sorted(self._processors.keys())]

    def supported_formats(self) -> list[str]:
        """Return all supported formats sorted alphabetically."""
        return sorted(self._format_map.keys())

    def clear(self) -> None:
        """Clear all registered processors and format mappings."""
        self._processors.clear()
        self._format_map.clear()


# Default process-level registry instance with reset capability for isolation
_default_registry: ProcessorRegistry | None = None


def get_default_registry() -> ProcessorRegistry:
    """Retrieve the process-level default processor registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ProcessorRegistry()
    return _default_registry


def reset_default_registry() -> None:
    """Reset the process-level default processor registry (primarily for test teardown)."""
    global _default_registry
    if _default_registry is not None:
        _default_registry.clear()
        _default_registry = None
