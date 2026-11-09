"""Document Processing Subsystem (Phase 4.1).

Provides an extensible, decoupled foundation for document processors,
format resolution, processing execution contexts, and standardized results.
"""

from app.processing.base import BaseProcessor
from app.processing.context import ProcessingContext
from app.processing.detector import DetectionResult, FormatDetector
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
from app.processing.registry import (
    ProcessorRegistry,
    get_default_registry,
    reset_default_registry,
)
from app.processing.result import (
    MAX_JSON_NESTING_DEPTH,
    PageResult,
    ProcessingResult,
)
from app.processing.stream import PrefixedStreamReader
from app.processing.types import (
    DocumentFormat,
    ProcessingCapability,
    ProcessingLimits,
    ProcessingStatus,
    ProcessorMetadata,
)

__all__ = [
    "MAX_JSON_NESTING_DEPTH",
    "BaseProcessor",
    "CorruptedDocumentError",
    "DetectionResult",
    "DocumentFormat",
    "EncryptedDocumentError",
    "FormatDetector",
    "InvalidProcessingInputError",
    "PageResult",
    "PrefixedStreamReader",
    "ProcessingCapability",
    "ProcessingContext",
    "ProcessingError",
    "ProcessingLimits",
    "ProcessingResourceLimitExceededError",
    "ProcessingResult",
    "ProcessingStatus",
    "ProcessorConfigurationError",
    "ProcessorExecutionError",
    "ProcessorMetadata",
    "ProcessorRegistrationError",
    "ProcessorRegistry",
    "ProcessorTimeoutError",
    "UnsupportedDocumentFormatError",
    "get_default_registry",
    "reset_default_registry",
]
