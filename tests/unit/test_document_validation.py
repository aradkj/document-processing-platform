"""Unit tests for document filename sanitization, content-signature detection, and MIME matching."""

from __future__ import annotations

import pytest

from app.services.document_validation import (
    EmptyFileError,
    ExtensionMismatchError,
    InvalidFilenameError,
    UnsupportedFileTypeError,
    detect_content_type,
    validate_and_sanitize_filename,
    validate_extension_matches_content,
)

# =====================================================================
# Filename Validation & Normalization Tests
# =====================================================================


def test_filename_valid_standard_names() -> None:
    """Verify standard valid filenames are accepted and normalized."""
    assert validate_and_sanitize_filename("report.pdf") == "report.pdf"
    assert validate_and_sanitize_filename("quarterly_2026.PDF") == "quarterly_2026.PDF"
    assert validate_and_sanitize_filename("user-avatar.png") == "user-avatar.png"
    assert validate_and_sanitize_filename("scan_001.jpg") == "scan_001.jpg"
    assert validate_and_sanitize_filename("document.archive.tiff") == "document.archive.tiff"
    assert validate_and_sanitize_filename("banner.webp") == "banner.webp"


def test_filename_valid_with_spaces() -> None:
    """Verify filenames containing internal spaces are preserved."""
    assert (
        validate_and_sanitize_filename("my quarterly report 2026.pdf")
        == "my quarterly report 2026.pdf"
    )
    assert (
        validate_and_sanitize_filename("  leading and trailing spaces.png  ")
        == "leading and trailing spaces.png"
    )


def test_filename_unicode_and_persian() -> None:
    """Verify international and Persian filenames are supported and NFC normalized."""
    persian_name = "گزارش_مالی_۱۴۰۳.pdf"
    assert validate_and_sanitize_filename(persian_name) == persian_name

    french_name = "résumé_officiel.pdf"
    assert validate_and_sanitize_filename(french_name) == french_name

    german_name = "Überweisung_Beleg.png"
    assert validate_and_sanitize_filename(german_name) == german_name


def test_filename_multiple_extensions() -> None:
    """Verify filenames with multiple dots/extensions are supported."""
    assert validate_and_sanitize_filename("backup.2026.tar.pdf") == "backup.2026.tar.pdf"
    assert validate_and_sanitize_filename("archive.v1.2.final.png") == "archive.v1.2.final.png"


@pytest.mark.parametrize(
    "invalid_filename",
    [
        None,
        "",
        "   ",
        "\t\n",
    ],
)
def test_filename_empty_or_none_rejected(invalid_filename: str | None) -> None:
    """Verify None, empty, and whitespace-only filenames are rejected."""
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename(invalid_filename)
    assert "empty" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_filename_null_byte_rejected() -> None:
    """Verify filenames containing null bytes are rejected."""
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename("innocent.pdf\0.exe")
    assert "null byte" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "char_code",
    [0, 1, 7, 8, 10, 13, 27, 31, 127],  # Bell, BS, LF, CR, ESC, DEL
)
def test_filename_control_characters_rejected(char_code: int) -> None:
    """Verify ASCII control characters are rejected."""
    bad_name = f"document_{chr(char_code)}.pdf"
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename(bad_name)
    assert (
        "control character" in str(exc_info.value).lower()
        or "null byte" in str(exc_info.value).lower()
    )


@pytest.mark.parametrize(
    "traversal_name",
    [
        "../secret.pdf",
        "..\\windows\\system32.pdf",
        "nested/sub/document.pdf",
        "nested\\sub\\document.pdf",
        "....//....//etc/passwd.pdf",
        "foo/../bar.pdf",
        "foo\\..\\bar.pdf",
    ],
)
def test_filename_path_traversal_and_separators_rejected(traversal_name: str) -> None:
    """Verify directory separators and path traversal attempts are strictly rejected."""
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename(traversal_name)
    err = str(exc_info.value).lower()
    assert "path" in err or "traversal" in err or "separator" in err


@pytest.mark.parametrize(
    "absolute_name",
    [
        "C:\\autoexec.bat.pdf",
        "c:/boot.ini.pdf",
        "D:\\data.pdf",
        "Z:/network/file.pdf",
    ],
)
def test_filename_windows_drive_letters_rejected(absolute_name: str) -> None:
    """Verify Windows drive letter patterns are rejected as absolute paths."""
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename(absolute_name)
    err = str(exc_info.value).lower()
    assert "absolute" in err or "path" in err or "separator" in err


def test_filename_length_boundary_255() -> None:
    """Verify 255-character maximum length boundary."""
    # 251 chars of stem + ".pdf" (4 chars) = 255 chars (exactly at limit)
    stem_251 = "a" * 251
    exact_255 = f"{stem_251}.pdf"
    assert len(exact_255) == 255
    assert validate_and_sanitize_filename(exact_255) == exact_255

    # 252 chars of stem + ".pdf" = 256 chars (exceeds limit)
    too_long = f"{'a' * 252}.pdf"
    assert len(too_long) == 256
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_and_sanitize_filename(too_long)
    assert "exceeds maximum allowed limit of 255 characters" in str(exc_info.value)


@pytest.mark.parametrize(
    "dot_name",
    [
        ".",
        "..",
        "...",
        ".pdf",
        "   .pdf   ",
        "document_without_extension",
        "trailing_dot.",
    ],
)
def test_filename_missing_stem_or_extension_rejected(dot_name: str) -> None:
    """Verify filenames missing either a stem or valid extension are rejected."""
    with pytest.raises(InvalidFilenameError):
        validate_and_sanitize_filename(dot_name)


# =====================================================================
# Content Signature / Magic Byte Detection Tests
# =====================================================================


def test_detect_content_type_pdf() -> None:
    """Verify PDF magic bytes (%PDF-) are detected accurately."""
    header = b"%PDF-1.7\r\n\x80\x81\x82..."
    assert detect_content_type(header) == "application/pdf"


def test_detect_content_type_jpeg() -> None:
    """Verify JPEG SOI marker (\\xFF\\xD8\\xFF) is detected accurately."""
    header = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01..."
    assert detect_content_type(header) == "image/jpeg"


def test_detect_content_type_png() -> None:
    """Verify PNG 8-byte signature is detected accurately."""
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."
    assert detect_content_type(header) == "image/png"


def test_detect_content_type_tiff_little_endian() -> None:
    """Verify TIFF Little-Endian (II*\\x00) signature is detected accurately."""
    header = b"II*\x00\x08\x00\x00\x00..."
    assert detect_content_type(header) == "image/tiff"


def test_detect_content_type_tiff_big_endian() -> None:
    """Verify TIFF Big-Endian (MM\\x00*) signature is detected accurately."""
    header = b"MM\x00*\x00\x00\x00\x08..."
    assert detect_content_type(header) == "image/tiff"


def test_detect_content_type_webp() -> None:
    """Verify WebP RIFF/WEBP signature is detected accurately."""
    # RIFF header: bytes 0..3: RIFF, bytes 4..7: file size, bytes 8..11: WEBP
    header = b"RIFF\x24\x00\x00\x00WEBPVP8 ..."
    assert detect_content_type(header) == "image/webp"


def test_detect_content_type_empty_file_rejected() -> None:
    """Verify empty byte buffer raises EmptyFileError."""
    with pytest.raises(EmptyFileError) as exc_info:
        detect_content_type(b"")
    assert "empty" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "corrupted_header",
    [
        b"%PDX-1.7",  # Corrupted PDF
        b"\xff\xd8\x00",  # Corrupted JPEG
        b"\x89PNG\r\n\x1a\x00",  # Corrupted PNG
        b"II\x00*",  # Corrupted TIFF
        b"RIFF\x10\x00\x00\x00AVIF",  # RIFF but not WEBP
        b"MZ\x90\x00",  # Windows PE executable
        b"\x7fELF",  # Linux ELF executable
        b"PK\x03\x04",  # Zip archive / DOCX / JAR
        b"<!DOCTYPE html>",  # HTML / Script
        b"{\x22title\x22: \x22json\x22}",  # JSON
        b"Just plain text without magic bytes",
        b"\x00\x01\x02\x03",
    ],
)
def test_detect_content_type_unsupported_rejected(corrupted_header: bytes) -> None:
    """Verify unrecognized or unsupported magic byte signatures are rejected."""
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        detect_content_type(corrupted_header)
    assert "unsupported" in str(exc_info.value).lower()


def test_detect_content_type_truncated_header_rejected() -> None:
    """Verify truncated headers that cannot satisfy signature requirements are rejected."""
    with pytest.raises(UnsupportedFileTypeError):
        detect_content_type(b"%P")
    with pytest.raises(UnsupportedFileTypeError):
        detect_content_type(b"\x89PNG")
    with pytest.raises(UnsupportedFileTypeError):
        detect_content_type(b"RIFF")  # Missing WEBP chunk marker


# =====================================================================
# Extension & Content Type Matching Tests
# =====================================================================


def test_extension_matching_success() -> None:
    """Verify matching filename extensions and detected MIME types pass."""
    validate_extension_matches_content("invoice.pdf", "application/pdf")
    validate_extension_matches_content("photo.jpg", "image/jpeg")
    validate_extension_matches_content("photo.jpeg", "image/jpeg")
    validate_extension_matches_content("chart.png", "image/png")
    validate_extension_matches_content("scan.tiff", "image/tiff")
    validate_extension_matches_content("scan.tif", "image/tiff")
    validate_extension_matches_content("graphic.webp", "image/webp")


@pytest.mark.parametrize(
    ("filename", "detected_mime"),
    [
        ("document.pdf", "image/png"),  # Extension claims PDF, content is PNG
        ("image.png", "application/pdf"),  # Extension claims PNG, content is PDF
        ("photo.jpg", "image/webp"),  # Extension claims JPG, content is WebP
        ("avatar.webp", "image/jpeg"),  # Extension claims WebP, content is JPEG
        ("script.sh", "application/pdf"),  # Extension claims SH, content is PDF
        ("exploit.exe", "image/png"),  # Extension claims EXE, content is PNG
    ],
)
def test_extension_spoofing_mismatch_rejected(filename: str, detected_mime: str) -> None:
    """Verify spoofed extensions that mismatch detected content signatures are rejected."""
    with pytest.raises(ExtensionMismatchError) as exc_info:
        validate_extension_matches_content(filename, detected_mime)
    assert "does not match detected content type" in str(exc_info.value)
