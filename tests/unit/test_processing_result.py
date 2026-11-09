"""Unit tests for ProcessingResult schema, serialization, and sanitization."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.processing.result import (
    CURRENT_SCHEMA_VERSION,
    MAX_JSON_NESTING_DEPTH,
    PageResult,
    ProcessingResult,
)
from app.processing.types import ProcessingStatus


def test_valid_processing_result_minimal() -> None:
    """Verify minimal valid ProcessingResult with defaults."""
    res = ProcessingResult(
        processor_name="pdf-extractor",
        processor_version="1.0.0",
    )
    assert res.processor_name == "pdf-extractor"
    assert res.processor_version == "1.0.0"
    assert res.schema_version == CURRENT_SCHEMA_VERSION
    assert res.status == ProcessingStatus.SUCCESS
    assert res.metadata == {}
    assert res.extracted_text is None
    assert res.pages is None
    assert res.warnings == []
    assert res.statistics == {}
    assert res.error_code is None
    assert res.error_message is None


def test_valid_processing_result_full() -> None:
    """Verify full ProcessingResult with pages and statistics."""
    pages = [
        PageResult(
            page_number=1,
            extracted_text="Page 1 text",
            width=612.0,
            height=792.0,
            metadata={"orientation": "portrait"},
        ),
        PageResult(
            page_number=2,
            extracted_text="Page 2 text",
            width=612.0,
            height=792.0,
        ),
    ]
    res = ProcessingResult(
        processor_name="pdf-extractor",
        processor_version="1.0.0",
        schema_version="1.0.0",
        status=ProcessingStatus.SUCCESS,
        metadata={"total_pages": 2, "author": "Alice"},
        extracted_text="Page 1 text\n\nPage 2 text",
        pages=pages,
        warnings=["Non-embedded font replaced"],
        statistics={"duration_ms": 125.4, "char_count": 22},
    )

    assert len(res.pages or []) == 2
    assert res.metadata["author"] == "Alice"
    assert res.statistics["duration_ms"] == 125.4


def test_deterministic_json_serialization() -> None:
    """Verify to_dict and to_json serialize deterministically."""
    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="2.1.0",
        metadata={"b_key": 2, "a_key": 1},
        statistics={"z": 9, "m": 5},
    )

    d = res.to_dict()
    assert isinstance(d, dict)
    assert d["processor_name"] == "test-proc"
    assert d["metadata"] == {"b_key": 2, "a_key": 1}

    json_str = res.to_json()
    assert isinstance(json_str, str)
    parsed = json.loads(json_str)
    assert parsed["processor_name"] == "test-proc"


def test_failure_status_requires_error_code() -> None:
    """Verify failed status without error_code raises validation error."""
    with pytest.raises(ValidationError, match="error_code must be provided"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            status=ProcessingStatus.FAILED,
            error_message="Something failed",
            # missing error_code
        )


def test_result_sanitizes_path_leakage() -> None:
    """Verify paths are scrubbed from error_message and warnings."""
    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        status=ProcessingStatus.FAILED,
        error_code="INTERNAL_ERROR",
        error_message="Cannot write temp file at C:\\docker\\OCR\\storage_data\\dump.bin",
        warnings=["Warning: /storage/data/secret.txt could not be indexed"],
    )
    assert "C:\\" not in (res.error_message or "")
    assert "/storage/" not in res.warnings[0]
    assert "[redacted_path]" in (res.error_message or "")
    assert "[redacted_path]" in res.warnings[0]


def test_extra_fields_forbidden() -> None:
    """Verify extra arbitrary fields are rejected by the schema."""
    with pytest.raises(ValidationError):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            extra_unsupported_field="attack",  # type: ignore[call-arg]
        )


def test_page_result_invalid_page_number() -> None:
    """Verify page_number must be >= 1."""
    with pytest.raises(ValidationError):
        PageResult(page_number=0)


@pytest.mark.parametrize(
    "bad_val",
    [
        object(),
        lambda x: x,
        {1, 2, 3},  # set is not standard JSON
    ],
)
def test_non_json_serializable_metadata_rejected(bad_val: object) -> None:
    """Verify that non-JSON-compatible values in metadata are rejected at construction."""
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata={"unsupported": bad_val},
        )


def test_non_json_serializable_statistics_rejected() -> None:
    """Verify that non-JSON-compatible values in statistics are rejected at construction."""
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            statistics={"invalid_metric": object()},
        )


def test_page_result_non_json_metadata_rejected() -> None:
    """Verify non-JSON-compatible values in PageResult metadata are rejected at construction."""
    with pytest.raises(ValidationError, match="not JSON-serializable"):
        PageResult(
            page_number=1,
            metadata={"bad_data": object()},
        )


def test_finite_floats_accepted() -> None:
    """Verify that finite floating-point numbers are accepted and serialize cleanly."""
    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        metadata={"ratio": 0.75, "zero": 0.0, "negative": -123.456},
        statistics={"duration_sec": 1.234, "cpu_usage": 45.6},
    )
    assert res.metadata["ratio"] == 0.75
    assert res.statistics["duration_sec"] == 1.234

    # Verify standard JSON serialization succeeds
    json_str = res.to_json()
    parsed = json.loads(json_str)
    assert parsed["metadata"]["ratio"] == 0.75
    assert parsed["statistics"]["duration_sec"] == 1.234


@pytest.mark.parametrize(
    "non_finite_float",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_non_finite_floats_rejected(non_finite_float: float) -> None:
    """Verify NaN, +inf, and -inf are rejected in metadata and statistics."""
    with pytest.raises(ValidationError, match="Non-finite float"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata={"invalid_num": non_finite_float},
        )

    with pytest.raises(ValidationError, match="Non-finite float"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            statistics={"invalid_metric": non_finite_float},
        )


@pytest.mark.parametrize(
    "non_finite_float",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_page_result_non_finite_floats_rejected(non_finite_float: float) -> None:
    """Verify NaN, +inf, and -inf are rejected in PageResult metadata."""
    with pytest.raises(ValidationError, match="Non-finite float"):
        PageResult(
            page_number=1,
            metadata={"invalid_num": non_finite_float},
        )


def test_circular_reference_dict_rejected() -> None:
    """Verify self-referential dictionaries are detected and rejected without recursion error."""
    d: dict[str, object] = {"a": 1}
    d["self"] = d

    with pytest.raises(ValidationError, match="Circular reference detected"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata=d,
        )


def test_circular_reference_list_rejected() -> None:
    """Verify self-referential lists are detected and rejected."""
    lst: list[object] = [1, 2]
    lst.append(lst)

    with pytest.raises(ValidationError, match="Circular reference detected"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata={"nested_list": lst},
        )


def test_circular_reference_indirect_rejected() -> None:
    """Verify indirect cyclic references across dictionaries and lists are rejected."""
    a: dict[str, object] = {}
    b: dict[str, object] = {"parent": a}
    a["child"] = b

    with pytest.raises(ValidationError, match="Circular reference detected"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata=a,
        )


def test_page_result_circular_reference_rejected() -> None:
    """Verify cyclic references are rejected in PageResult metadata."""
    d: dict[str, object] = {}
    d["loop"] = d

    with pytest.raises(ValidationError, match="Circular reference detected"):
        PageResult(
            page_number=1,
            metadata=d,
        )


def test_repeated_references_dag_accepted() -> None:
    """Verify repeated non-circular references (DAG) are accepted and serialize properly."""
    shared_sub_dict = {"shared_key": "shared_value", "count": 42}
    dag_metadata = {
        "ref1": shared_sub_dict,
        "ref2": shared_sub_dict,
        "list_refs": [shared_sub_dict, shared_sub_dict],
    }

    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        metadata=dag_metadata,
    )
    assert res.metadata["ref1"]["shared_key"] == "shared_value"

    # Must serialize to standard JSON without error
    json_str = res.to_json()
    parsed = json.loads(json_str)
    assert parsed["metadata"]["ref1"] == parsed["metadata"]["ref2"]


def test_validator_does_not_mutate_caller_objects() -> None:
    """Verify that checking serializability leaves caller data structures untouched."""
    original_dict = {
        "key1": "val1",
        "nested": {"inner": [1, 2, 3]},
    }
    dict_repr_before = repr(original_dict)

    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        metadata=original_dict,
    )
    assert repr(original_dict) == dict_repr_before
    assert res.metadata["key1"] == "val1"


def _build_nested_dict(levels: int, leaf: Any = 1) -> dict[str, Any]:
    """Helper creating a recursively nested dictionary tree."""
    cur: Any = leaf
    for _ in range(levels):
        cur = {"nested": cur}
    return cur  # type: ignore[no-any-return]


def test_json_nesting_depth_immediately_below_limit() -> None:
    """Verify structure at the exact nesting limit (64 levels) is accepted and serializes."""
    nested_data = _build_nested_dict(MAX_JSON_NESTING_DEPTH, leaf="deep_leaf")
    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        metadata=nested_data,
    )
    # Confirm deterministic JSON serialization works at the limit
    json_str = res.to_json()
    assert "deep_leaf" in json_str


def test_json_nesting_depth_exceeding_limit_rejected() -> None:
    """Verify nesting exceeding MAX_JSON_NESTING_DEPTH raises ValidationError."""
    too_deep = _build_nested_dict(MAX_JSON_NESTING_DEPTH + 1, leaf="too_deep_leaf")
    with pytest.raises(ValidationError, match="Exceeded maximum JSON nesting depth"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata=too_deep,
        )


def test_json_nesting_depth_exceeded_in_statistics() -> None:
    """Verify deep nesting in statistics is also intercepted."""
    too_deep = _build_nested_dict(MAX_JSON_NESTING_DEPTH + 1)
    with pytest.raises(ValidationError, match="Exceeded maximum JSON nesting depth"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            statistics=too_deep,
        )


def test_page_result_json_nesting_depth_exceeded() -> None:
    """Verify deep nesting in PageResult metadata is intercepted."""
    too_deep = _build_nested_dict(MAX_JSON_NESTING_DEPTH + 1)
    with pytest.raises(ValidationError, match="Exceeded maximum JSON nesting depth"):
        PageResult(
            page_number=1,
            metadata=too_deep,
        )


def test_deeply_nested_circular_reference_rejected() -> None:
    """Verify that a circular reference placed deep inside nested containers is intercepted."""
    nested = _build_nested_dict(20, leaf={"leaf_key": "val"})
    # Find innermost dict and insert cycle pointing back to top
    target = nested
    for _ in range(19):
        target = target["nested"]
    target["loop"] = nested

    with pytest.raises(ValidationError, match="Circular reference detected"):
        ProcessingResult(
            processor_name="test-proc",
            processor_version="1.0.0",
            metadata=nested,
        )


def test_repeated_references_near_nesting_limit() -> None:
    """Verify that multiple sibling references near depth limit do not trigger false cycle."""
    shared_deep = {"shared_deep_key": "val"}
    # Construct a parent with 20 levels of nesting containing repeated sibling references
    sub_tree = _build_nested_dict(50, leaf=[shared_deep, shared_deep, shared_deep])
    res = ProcessingResult(
        processor_name="test-proc",
        processor_version="1.0.0",
        metadata={"branch1": sub_tree, "branch2": sub_tree},
    )
    assert res.metadata["branch1"] == res.metadata["branch2"]
