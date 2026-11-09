"""Unit tests for processing exception hierarchy and error code safety."""

from __future__ import annotations

import pytest

from app.processing.exceptions import (
    CorruptedDocumentError,
    EncryptedDocumentError,
    InvalidProcessingInputError,
    ProcessingError,
    ProcessingResourceLimitExceededError,
    ProcessorConfigurationError,
    ProcessorExecutionError,
    ProcessorRegistrationError,
    ProcessorTimeoutError,
    UnsupportedDocumentFormatError,
)


def test_exception_inheritance_hierarchy() -> None:
    """Verify all domain exceptions inherit from ProcessingError."""
    assert issubclass(ProcessorConfigurationError, ProcessingError)
    assert issubclass(ProcessorRegistrationError, ProcessingError)
    assert issubclass(UnsupportedDocumentFormatError, ProcessingError)
    assert issubclass(InvalidProcessingInputError, ProcessingError)
    assert issubclass(CorruptedDocumentError, ProcessingError)
    assert issubclass(EncryptedDocumentError, ProcessingError)
    assert issubclass(ProcessingResourceLimitExceededError, ProcessingError)
    assert issubclass(ProcessorTimeoutError, ProcessingResourceLimitExceededError)
    assert issubclass(ProcessorExecutionError, ProcessingError)


def test_exception_default_codes() -> None:
    """Verify stable machine-readable error codes."""
    assert ProcessingError("error").code == "PROCESSING_ERROR"
    assert ProcessorConfigurationError("error").code == "PROCESSOR_CONFIGURATION_ERROR"
    assert ProcessorRegistrationError("error").code == "PROCESSOR_REGISTRATION_ERROR"
    assert UnsupportedDocumentFormatError("error").code == "UNSUPPORTED_DOCUMENT_FORMAT"
    assert InvalidProcessingInputError("error").code == "INVALID_PROCESSING_INPUT"
    assert CorruptedDocumentError("error").code == "CORRUPTED_DOCUMENT"
    assert EncryptedDocumentError("error").code == "ENCRYPTED_DOCUMENT"
    assert (
        ProcessingResourceLimitExceededError("error").code == "PROCESSING_RESOURCE_LIMIT_EXCEEDED"
    )
    assert ProcessorTimeoutError("error").code == "PROCESSOR_TIMEOUT"
    assert ProcessorExecutionError("error").code == "PROCESSOR_EXECUTION_ERROR"


def test_exception_to_dict_structure() -> None:
    """Verify to_dict returns safe structured information."""
    exc = UnsupportedDocumentFormatError(
        "Format is unsupported",
        details={"requested_format": "application/x-unknown"},
    )
    d = exc.to_dict()
    assert d["code"] == "UNSUPPORTED_DOCUMENT_FORMAT"
    assert d["message"] == "Format is unsupported"
    assert d["details"] == {"requested_format": "application/x-unknown"}


@pytest.mark.parametrize(
    ("raw_msg", "forbidden_leaks"),
    [
        (
            "Failed to read file at C:\\docker\\OCR\\storage_data\\obj.bin",
            ("C:\\", "docker\\OCR", "storage_data"),
        ),
        (
            "Error accessing D:/app/documents/report.pdf on host",
            ("D:/", "app/documents", "report.pdf"),
        ),
        (
            "File not found on network share \\\\server\\share\\storage\\doc.pdf",
            ("\\\\server", "share\\storage", "doc.pdf"),
        ),
        (
            "Unable to access //smbserver/share/sub/file.dat",
            ("//smbserver", "share/sub", "file.dat"),
        ),
        (
            "Error accessing /storage/documents/secret.pdf on host",
            ("/storage", "documents/secret.pdf"),
        ),
        (
            "File not found in /var/lib/data/file.pdf",
            ("/var/lib", "data/file.pdf"),
        ),
        (
            "Permission denied for /tmp/scratch/temp.pdf",
            ("/tmp/scratch", "temp.pdf"),
        ),
        (
            "Failed opening ./storage_data/ab/cd/file.dat",
            ("./storage_data", "ab/cd"),
        ),
        (
            "Could not open storage_data/ab/cd/12345.bin",
            ("storage_data", "ab/cd"),
        ),
    ],
)
def test_exception_message_path_scrubbing(raw_msg: str, forbidden_leaks: tuple[str, ...]) -> None:
    """Verify internal filesystem paths and subdirectories are scrubbed from error messages."""
    exc = ProcessorExecutionError(raw_msg)
    for forbidden in forbidden_leaks:
        assert forbidden.lower() not in exc.message.lower()
    assert "[redacted_path]" in exc.message


def test_mime_types_not_redacted() -> None:
    """Verify standard MIME types are not accidentally scrubbed by path sanitization."""
    exc = UnsupportedDocumentFormatError(
        "Format 'application/pdf' or 'image/jpeg' is not supported."
    )
    assert "application/pdf" in exc.message
    assert "image/jpeg" in exc.message
    assert "[redacted_path]" not in exc.message
