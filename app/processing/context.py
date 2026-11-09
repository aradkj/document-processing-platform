"""Processing context contract providing immutable, typed execution state."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.processing.exceptions import InvalidProcessingInputError
from app.processing.types import ProcessingLimits
from app.storage.base import StoredObjectReader

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ProcessingContext:
    """Execution context supplied to document processors.

    Designed to be completely decoupled from HTTP requests, FastAPI, and ORM sessions.
    Provides immutable metadata, streaming reader access, and resource boundaries.

    Immutability & Statefulness Notes:
    - Shallow immutability: The top-level `metadata` mapping is wrapped in `MappingProxyType`.
      Nested mutable collections within metadata (if any) are not recursively deep-frozen.
    - Reader statefulness: `reader` conforms to `StoredObjectReader`, which is an inherently
      stateful streaming construct (file descriptors, internal buffers, `.closed` flag).
      When format detection precedes processing, `reader` must be a replay-capable reader
      (e.g., `PrefixedStreamReader` returned by `FormatDetector.detect()`) to ensure the
      stream can be read from byte zero.
    """

    document_id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    reader: StoredObjectReader
    limits: ProcessingLimits = field(default_factory=ProcessingLimits)
    correlation_id: str = field(default_factory=lambda: secrets.token_hex(16))
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Validate context integrity constraints upon construction."""
        if not isinstance(self.document_id, uuid.UUID):
            raise InvalidProcessingInputError(
                f"document_id must be a valid uuid.UUID, got {type(self.document_id).__name__}."
            )

        if not self.original_filename or not self.original_filename.strip():
            raise InvalidProcessingInputError("original_filename cannot be empty.")

        if not self.content_type or "/" not in self.content_type:
            raise InvalidProcessingInputError(
                f"content_type must be a valid MIME type, got '{self.content_type}'."
            )

        if self.size_bytes < 0:
            raise InvalidProcessingInputError(
                f"size_bytes must be non-negative, got {self.size_bytes}."
            )

        cleaned_sha = self.sha256.strip().lower()
        if not _SHA256_HEX_PATTERN.match(cleaned_sha):
            raise InvalidProcessingInputError(
                f"sha256 must be a 64-character hexadecimal digest, got '{self.sha256}'."
            )

        if not isinstance(self.reader, StoredObjectReader):
            reader_type = type(self.reader).__name__
            raise InvalidProcessingInputError(
                f"reader must implement StoredObjectReader interface, got {reader_type}."
            )

        if not isinstance(self.limits, ProcessingLimits):
            raise InvalidProcessingInputError(
                f"limits must be a ProcessingLimits instance, got {type(self.limits).__name__}."
            )

        if not self.correlation_id or not self.correlation_id.strip():
            raise InvalidProcessingInputError("correlation_id cannot be empty.")

        # Ensure metadata is an immutable mapping
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

        # Normalize sha256 to lowercase
        if self.sha256 != cleaned_sha:
            object.__setattr__(self, "sha256", cleaned_sha)

    def is_size_exceeded(self) -> bool:
        """Check whether the document size exceeds configured processing limits."""
        return self.size_bytes > self.limits.max_input_size_bytes
