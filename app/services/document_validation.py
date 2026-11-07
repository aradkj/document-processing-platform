"""Validation logic for document filenames, content signatures, and MIME types."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class DocumentValidationError(Exception):
    """Base exception for all document validation failures."""


class InvalidFilenameError(DocumentValidationError):
    """Raised when an uploaded filename violates security or format constraints."""


class EmptyFileError(DocumentValidationError):
    """Raised when an uploaded file contains zero bytes."""


class UnsupportedFileTypeError(DocumentValidationError):
    """Raised when file content signature does not match any allowed format."""


class ExtensionMismatchError(DocumentValidationError):
    """Raised when the file extension does not match the detected MIME content type."""


@dataclass(frozen=True, slots=True)
class SupportedFormat:
    """Specification of an allowed document/image format."""

    mime_type: str
    extensions: tuple[str, ...]
    description: str


SUPPORTED_FORMATS: dict[str, SupportedFormat] = {
    "application/pdf": SupportedFormat(
        mime_type="application/pdf",
        extensions=(".pdf",),
        description="Portable Document Format",
    ),
    "image/jpeg": SupportedFormat(
        mime_type="image/jpeg",
        extensions=(".jpg", ".jpeg"),
        description="JPEG image",
    ),
    "image/png": SupportedFormat(
        mime_type="image/png",
        extensions=(".png",),
        description="Portable Network Graphics",
    ),
    "image/tiff": SupportedFormat(
        mime_type="image/tiff",
        extensions=(".tiff", ".tif"),
        description="Tagged Image File Format",
    ),
    "image/webp": SupportedFormat(
        mime_type="image/webp",
        extensions=(".webp",),
        description="WebP image",
    ),
}

# Windows drive letter pattern (e.g. C:, D:)
_DRIVE_LETTER_REGEX = re.compile(r"^[a-zA-Z]:")


def validate_and_sanitize_filename(filename: str | None) -> str:
    """Validate and normalize an untrusted user-supplied filename.

    Enforces:
    - Non-null, non-empty, non-whitespace string.
    - No null bytes (\0).
    - No ASCII control characters (ord < 32 or ord == 127).
    - No directory separators (/ or \\).
    - No path traversal sequences (..).
    - No absolute path markers (POSIX leading slash or Windows drive letters).
    - Maximum length of 255 characters (PostgreSQL column limit).
    - Unicode normalization (NFC) preserving international characters (Persian, Arabic, etc.).
    - Must possess a valid extension.
    """
    if filename is None:
        raise InvalidFilenameError("Filename cannot be missing or null.")

    stripped = filename.strip()
    if not stripped:
        raise InvalidFilenameError("Filename cannot be empty or whitespace-only.")

    if "\0" in stripped or "%00" in stripped.lower():
        raise InvalidFilenameError("Filename contains invalid null byte.")

    # Reject control characters
    for char in stripped:
        code = ord(char)
        if code < 32 or code == 127:
            raise InvalidFilenameError("Filename contains invalid control characters.")

    # Reject directory separators
    if "/" in stripped or "\\" in stripped:
        raise InvalidFilenameError(
            "Filename cannot contain path separators or directory components."
        )

    # Reject path traversal
    if ".." in stripped:
        raise InvalidFilenameError("Filename cannot contain path traversal sequences ('..').")

    # Reject Windows drive letters and alternate data streams (colon)
    if ":" in stripped:
        raise InvalidFilenameError("Filename cannot contain colon or drive letter characters.")

    # Normalize Unicode to canonical form NFC
    normalized = unicodedata.normalize("NFC", stripped)

    # Enforce database column size limit
    if len(normalized) > 255:
        raise InvalidFilenameError(
            f"Filename length ({len(normalized)}) exceeds maximum allowed limit of 255 characters."
        )

    # Must contain a valid filename stem and extension
    pure_dots = normalized.strip(".")
    if not pure_dots:
        raise InvalidFilenameError("Filename cannot consist solely of dots.")

    extension = Path(normalized).suffix
    if not extension or extension == ".":
        raise InvalidFilenameError("Filename must include a valid file extension.")

    # Ensure there is a base name before the extension
    stem = Path(normalized).stem
    if not stem or not stem.strip():
        raise InvalidFilenameError("Filename must include a valid base name before the extension.")

    return normalized


def detect_content_type(header: bytes) -> str:
    """Determine the actual media type using magic byte content signatures.

    Inspects the initial bytes of the stream against a conservative allowlist.
    Client-provided Content-Type headers must NOT be trusted.

    Supported signatures:
    - PDF: starts with b"%PDF-"
    - JPEG: starts with b"\\xff\\xd8\\xff"
    - PNG: starts with b"\\x89PNG\\r\\n\\x1a\\n"
    - TIFF: starts with b"II*\\x00" (little-endian) or b"MM\\x00*" (big-endian)
    - WebP: starts with b"RIFF" (bytes 0-3) and b"WEBP" (bytes 8-11)
    """
    if not header:
        raise EmptyFileError("Uploaded file is empty.")

    # 1. PDF
    if header.startswith(b"%PDF-"):
        return "application/pdf"

    # 2. PNG (8-byte signature)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # 3. JPEG (3-byte SOI marker)
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # 4. TIFF (4-byte endianness and magic 42)
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "image/tiff"

    # 5. WebP (RIFF container with WEBP chunk type)
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"

    raise UnsupportedFileTypeError(
        "Unsupported file format or unrecognized content signature. "
        "Allowed formats: PDF, JPEG, PNG, TIFF, WebP."
    )


def validate_extension_matches_content(filename: str, detected_mime: str) -> None:
    """Validate that the normalized filename's extension matches the detected MIME type."""
    ext = Path(filename).suffix.lower()
    format_spec = SUPPORTED_FORMATS.get(detected_mime)

    if format_spec is None:
        raise UnsupportedFileTypeError(f"Unsupported media type: '{detected_mime}'.")

    if ext not in format_spec.extensions:
        allowed = ", ".join(format_spec.extensions)
        raise ExtensionMismatchError(
            f"File extension '{ext}' does not match detected content type '{detected_mime}' "
            f"(expected: {allowed})."
        )
