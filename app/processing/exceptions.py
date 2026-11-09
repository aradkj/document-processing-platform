"""Processing subsystem exception hierarchy with safe error codes and messages."""

from __future__ import annotations

import re
from typing import Any

# Patterns to detect potential filesystem paths that must not leak to external clients
_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows drive paths: C:\dir\file.ext or D:/path/file.ext
    re.compile(r"[a-zA-Z]:[\\/][^\s'\"<>\(\)\[\]\{\}]+", re.IGNORECASE),
    # Windows UNC network paths: \\server\share\file.ext or //server/share/...
    re.compile(r"(?:\\\\|//)[a-zA-Z0-9._-]+[\\/][^\s'\"<>\(\)\[\]\{\}]+", re.IGNORECASE),
    # Relative paths with ./ or ../ or storage paths: ./storage_data/..., storage_data/ab/cd/...
    re.compile(
        r"(?:\bstorage[a-zA-Z0-9_-]*|\.{1,2})[\\/][^\s'\"<>\(\)\[\]\{\}]+",
        re.IGNORECASE,
    ),
    # POSIX absolute paths: /var/..., /tmp/..., /storage/..., etc.
    re.compile(
        r"/(?:var|tmp|etc|usr|opt|storage|home|root|app|docker|srv|mnt|data)[^\s'\"<>\(\)\[\]\{\}]*",
        re.IGNORECASE,
    ),
    # General deep POSIX paths: /foo/bar/baz
    re.compile(r"/(?:[a-zA-Z0-9._-]+/){2,}[^\s'\"<>\(\)\[\]\{\}]*", re.IGNORECASE),
)


def _sanitize_message(message: str) -> str:
    """Scrub potential filesystem paths and sensitive prefixes from error messages."""
    for pattern in _PATH_PATTERNS:
        message = pattern.sub("[redacted_path]", message)
    return message


class ProcessingError(Exception):
    """Base exception for all document processing operations."""

    code: str = "PROCESSING_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        clean_msg = _sanitize_message(message)
        super().__init__(clean_msg)
        self.message = clean_msg
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return safe, structured representation suitable for logging or API mapping."""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class ProcessorConfigurationError(ProcessingError):
    """Raised when processor configuration is invalid, incomplete, or missing."""

    code = "PROCESSOR_CONFIGURATION_ERROR"


class ProcessorRegistrationError(ProcessingError):
    """Raised when processor registration fails (duplicate name, collision, or invalid metadata)."""

    code = "PROCESSOR_REGISTRATION_ERROR"


class UnsupportedDocumentFormatError(ProcessingError):
    """Raised when a document format or MIME type cannot be handled by any registered processor."""

    code = "UNSUPPORTED_DOCUMENT_FORMAT"


class InvalidProcessingInputError(ProcessingError):
    """Raised when input parameters, context fields, or metadata are invalid or inconsistent."""

    code = "INVALID_PROCESSING_INPUT"


class CorruptedDocumentError(ProcessingError):
    """Raised when document content stream is structurally corrupted, incomplete, or unparseable."""

    code = "CORRUPTED_DOCUMENT"


class EncryptedDocumentError(ProcessingError):
    """Raised when document is password-protected or encrypted and cannot be read."""

    code = "ENCRYPTED_DOCUMENT"


class ProcessingResourceLimitExceededError(ProcessingError):
    """Raised when processing exceeds configured limits (size, pages, duration, pixels)."""

    code = "PROCESSING_RESOURCE_LIMIT_EXCEEDED"


class ProcessorTimeoutError(ProcessingResourceLimitExceededError):
    """Raised when processor execution exceeds the configured timeout threshold."""

    code = "PROCESSOR_TIMEOUT"


class ProcessorExecutionError(ProcessingError):
    """Raised when an internal error occurs during processor execution."""

    code = "PROCESSOR_EXECUTION_ERROR"
