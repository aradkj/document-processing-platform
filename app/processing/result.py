"""Processing result contract and serialization schemas."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.processing.exceptions import _sanitize_message
from app.processing.types import ProcessingStatus

CURRENT_SCHEMA_VERSION = "1.0.0"
MAX_JSON_NESTING_DEPTH = 64


def _check_json_serializable(
    obj: Any,
    seen: set[int] | None = None,
    current_depth: int = 0,
) -> None:
    """Recursively verify that an object contains only JSON-serializable primitives.

    Rejects:
    - Nesting depth exceeding MAX_JSON_NESTING_DEPTH (preventing stack exhaustion)
    - Non-finite floats (NaN, +Infinity, -Infinity)
    - Circular references in dicts, lists, or tuples
    - Non-string dictionary keys
    - Types not serializable to standard JSON (e.g., sets, callables, custom objects)
    """
    if current_depth > MAX_JSON_NESTING_DEPTH:
        raise ValueError(f"Exceeded maximum JSON nesting depth of {MAX_JSON_NESTING_DEPTH} levels.")

    if seen is None:
        seen = set()

    if obj is None or isinstance(obj, (str, bool)):
        return

    if isinstance(obj, int):
        return

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError(f"Non-finite float '{obj}' is not JSON-serializable.")
        return

    obj_id = id(obj)
    if obj_id in seen:
        raise ValueError("Circular reference detected in data structure.")

    seen.add(obj_id)
    next_depth = current_depth + 1
    try:
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _check_json_serializable(item, seen, next_depth)
            return

        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str):
                    raise ValueError(f"Dictionary keys must be strings, got {type(k).__name__}")
                _check_json_serializable(v, seen, next_depth)
            return
    finally:
        seen.remove(obj_id)

    raise ValueError(f"Value of type '{type(obj).__name__}' is not JSON-serializable.")


class PageResult(BaseModel):
    """Structured extraction output for an individual document page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_number: int = Field(ge=1, description="1-indexed page number")
    extracted_text: str | None = Field(
        default=None, description="Extracted plain text for this page"
    )
    width: float | None = Field(default=None, ge=0.0, description="Page width in points or pixels")
    height: float | None = Field(
        default=None, ge=0.0, description="Page height in points or pixels"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Page-specific metadata")

    @field_validator("metadata")
    @classmethod
    def validate_metadata_serializable(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Ensure page metadata contains only JSON-serializable primitives."""
        _check_json_serializable(v)
        return v


class ProcessingResult(BaseModel):
    """Standardized, versioned outcome of a document processing operation.

    Guarantees deterministic JSON serialization, explicit optional fields,
    and safe exclusion of binary streams, raw exceptions, or filesystem paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    processor_name: str = Field(description="Identifier of the executing processor")
    processor_version: str = Field(description="Semantic version of the executing processor")
    schema_version: str = Field(
        default=CURRENT_SCHEMA_VERSION, description="Result schema format version"
    )
    status: ProcessingStatus = Field(
        default=ProcessingStatus.SUCCESS, description="Execution status"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extracted document/format metadata"
    )
    extracted_text: str | None = Field(default=None, description="Aggregated extracted plain text")
    pages: list[PageResult] | None = Field(
        default=None, description="Optional per-page structured extraction"
    )
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings encountered")
    statistics: dict[str, Any] = Field(
        default_factory=dict, description="Execution timing and metrics"
    )
    error_code: str | None = Field(
        default=None, description="Machine-readable error code on failure"
    )
    error_message: str | None = Field(
        default=None, description="Safe client-facing error description on failure"
    )

    @field_validator("metadata", "statistics")
    @classmethod
    def validate_json_serializable(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Ensure metadata and statistics contain only JSON-serializable primitives."""
        _check_json_serializable(v)
        return v

    @field_validator("error_message")
    @classmethod
    def sanitize_error_message(cls, v: str | None) -> str | None:
        """Sanitize error messages to prevent filesystem path disclosure."""
        if v is None:
            return None
        return _sanitize_message(v)

    @field_validator("warnings")
    @classmethod
    def sanitize_warnings(cls, v: list[str]) -> list[str]:
        """Sanitize warning messages to prevent filesystem path disclosure."""
        return [_sanitize_message(w) for w in v]

    @model_validator(mode="after")
    def validate_failure_consistency(self) -> ProcessingResult:
        """Validate consistency between status and error fields."""
        if self.status == ProcessingStatus.FAILED:
            if not self.error_code:
                raise ValueError("error_code must be provided when status is FAILED.")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Export result as a deterministic dictionary."""
        return self.model_dump(mode="json")

    def to_json(self, indent: int | None = None) -> str:
        """Export result as deterministic JSON string."""
        data = self.to_dict()
        return json.dumps(data, indent=indent, sort_keys=True)
