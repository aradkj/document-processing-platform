"""Format detection foundation verifying stored objects at processing time."""

from __future__ import annotations

from typing import NamedTuple

from app.processing.exceptions import (
    CorruptedDocumentError,
    UnsupportedDocumentFormatError,
)
from app.processing.stream import PrefixedStreamReader
from app.services.document_validation import (
    EmptyFileError,
    UnsupportedFileTypeError,
    detect_content_type,
)
from app.storage.base import StoredObjectReader

DEFAULT_DETECTION_BUFFER_SIZE = 65536  # 64 KiB inspection buffer


class DetectionResult(NamedTuple):
    """Result of format detection containing verified media type and replay-capable reader."""

    content_type: str
    reader: StoredObjectReader


class FormatDetector:
    """Verifies and determines the true media type of stored document streams.

    Prevents blind reliance on client-supplied headers, file extensions,
    or previously stored metadata. Inspects stream magic bytes while guaranteeing
    that downstream processors receive a replay-capable reader starting from byte zero.
    """

    def __init__(self, buffer_size: int = DEFAULT_DETECTION_BUFFER_SIZE) -> None:
        self.buffer_size = buffer_size

    async def detect(self, reader: StoredObjectReader) -> DetectionResult:
        """Asynchronously inspect the stored object stream and determine its MIME type.

        Args:
            reader: StoredObjectReader providing access to the object stream.

        Returns:
            DetectionResult(content_type, reader) where reader is a PrefixedStreamReader
            that replays the inspected header bytes before reading the rest of the stream.

        Raises:
            CorruptedDocumentError: If the stream is empty or truncated.
            UnsupportedDocumentFormatError: If the content signature is not recognized.
        """
        try:
            # Read inspection buffer from reader
            header = await reader.read(self.buffer_size)
            mime_type = detect_content_type(header)
            prefixed_reader = PrefixedStreamReader(prefix=header, inner=reader)
            return DetectionResult(content_type=mime_type, reader=prefixed_reader)
        except EmptyFileError as exc:
            raise CorruptedDocumentError("Stored document stream is empty.") from exc
        except UnsupportedFileTypeError as exc:
            raise UnsupportedDocumentFormatError(str(exc)) from exc

    def detect_sync(self, reader: StoredObjectReader) -> DetectionResult:
        """Synchronously inspect the stored object stream and determine its MIME type.

        Args:
            reader: StoredObjectReader providing access to the object stream.

        Returns:
            DetectionResult(content_type, reader) with replay-capable reader.

        Raises:
            CorruptedDocumentError: If the stream is empty or truncated.
            UnsupportedDocumentFormatError: If the content signature is not recognized.
        """
        try:
            header = reader.read_sync(self.buffer_size)
            mime_type = detect_content_type(header)
            prefixed_reader = PrefixedStreamReader(prefix=header, inner=reader)
            return DetectionResult(content_type=mime_type, reader=prefixed_reader)
        except EmptyFileError as exc:
            raise CorruptedDocumentError("Stored document stream is empty.") from exc
        except UnsupportedFileTypeError as exc:
            raise UnsupportedDocumentFormatError(str(exc)) from exc
