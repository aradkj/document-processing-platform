"""Unit tests for DocumentProcessing model, validation, and lifecycle state machine."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.document import Document
from app.db.models.document_processing import (
    ALLOWED_PROCESSING_TRANSITIONS,
    TERMINAL_PROCESSING_STATUSES,
    DocumentProcessing,
    ProcessingLifecycleStatus,
)
from app.processing.result import ProcessingResult
from app.services.processing_lifecycle import (
    DocumentNotFoundError,
    DocumentPendingDeletionError,
    InvalidProcessingTransitionError,
    ProcessingLifecycleService,
    ProcessingTerminalStateError,
    ProcessingValidationError,
)

SAMPLE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_document_processing_model_defaults_and_properties() -> None:
    """Verify default fields, active/terminal flags, and duration calculations."""
    doc_id = uuid.uuid4()
    proc_id = uuid.uuid4()
    now = datetime.now(UTC)

    run = DocumentProcessing(
        id=proc_id,
        document_id=doc_id,
        processor_name="pdf-extractor",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        created_at=now,
        updated_at=now,
    )

    assert run.id == proc_id
    assert run.document_id == doc_id
    assert run.processor_name == "pdf-extractor"
    assert run.processor_version == "1.0.0"
    assert run.input_sha256 == SAMPLE_SHA256
    assert run.status == ProcessingLifecycleStatus.PENDING.value
    assert run.started_at is None
    assert run.finished_at is None
    assert run.result is None
    assert run.error_code is None
    assert run.error_message is None
    assert run.is_active is True
    assert run.is_terminal is False
    assert run.duration_seconds is None
    assert "pdf-extractor:1.0.0" in repr(run)


def test_document_processing_duration_calculation() -> None:
    """Verify duration_seconds accurately computes runtime once finished."""
    t0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=12.5)

    run = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="ocr-proc",
        processor_version="2.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.SUCCEEDED.value,
        started_at=t0,
        finished_at=t1,
    )

    assert run.duration_seconds == 12.5
    assert run.is_terminal is True
    assert run.is_active is False


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "   ",
        "has space",
        "has/slash",
        "has@char",
        "a" * 65,  # Exceeds max length
    ],
)
@pytest.mark.asyncio
async def test_create_execution_invalid_processor_name_rejected(bad_name: str) -> None:
    """Verify validation rejects invalid processor name patterns."""
    session = AsyncMock()
    with pytest.raises(ProcessingValidationError, match="processor_name"):
        await ProcessingLifecycleService.create_execution(
            session,
            document_id=uuid.uuid4(),
            processor_name=bad_name,
            processor_version="1.0.0",
            input_sha256=SAMPLE_SHA256,
        )


@pytest.mark.parametrize(
    "bad_version",
    [
        "",
        "   ",
        "v1.0",
        "1.0",
        "beta",
        "1.a.2",
        "1.0.0-" + ("a" * 30),  # Exceeds max length 32
    ],
)
@pytest.mark.asyncio
async def test_create_execution_invalid_version_rejected(bad_version: str) -> None:
    """Verify validation rejects non-semver processor versions."""
    session = AsyncMock()
    with pytest.raises(ProcessingValidationError, match="processor_version"):
        await ProcessingLifecycleService.create_execution(
            session,
            document_id=uuid.uuid4(),
            processor_name="valid-name",
            processor_version=bad_version,
            input_sha256=SAMPLE_SHA256,
        )


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",
        "abc",
        "Z" * 64,  # Non-hex
        "a" * 63,  # Too short
        "a" * 65,  # Too long
        SAMPLE_SHA256.upper(),  # Uppercase not allowed
    ],
)
@pytest.mark.asyncio
async def test_create_execution_invalid_sha256_rejected(bad_sha: str) -> None:
    """Verify input SHA-256 must be exactly 64 lowercase hex characters."""
    session = AsyncMock()
    with pytest.raises(ProcessingValidationError, match="input_sha256"):
        await ProcessingLifecycleService.create_execution(
            session,
            document_id=uuid.uuid4(),
            processor_name="valid-name",
            processor_version="1.0.0",
            input_sha256=bad_sha,
        )


@pytest.mark.asyncio
async def test_create_execution_missing_document_raises() -> None:
    """Verify DocumentNotFoundError when target document ID does not exist."""
    session = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_res

    doc_id = uuid.uuid4()
    with pytest.raises(DocumentNotFoundError, match=str(doc_id)):
        await ProcessingLifecycleService.create_execution(
            session,
            document_id=doc_id,
            processor_name="pdf-extractor",
            processor_version="1.0.0",
            input_sha256=SAMPLE_SHA256,
        )


@pytest.mark.asyncio
async def test_create_execution_document_pending_deletion_raises() -> None:
    """Verify DocumentPendingDeletionError when document is marked for deletion."""
    session = AsyncMock()
    mock_doc = MagicMock(spec=Document)
    mock_doc.id = uuid.uuid4()
    mock_doc.deletion_pending_at = datetime.now(UTC)

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_doc
    session.execute.return_value = mock_res

    with pytest.raises(DocumentPendingDeletionError, match="pending deletion"):
        await ProcessingLifecycleService.create_execution(
            session,
            document_id=mock_doc.id,
            processor_name="pdf-extractor",
            processor_version="1.0.0",
            input_sha256=SAMPLE_SHA256,
        )


def test_lifecycle_allowed_and_forbidden_transitions() -> None:
    """Verify the strict transition matrix for the lifecycle state machine."""
    # From PENDING: only RUNNING and CANCELLED
    assert ALLOWED_PROCESSING_TRANSITIONS[ProcessingLifecycleStatus.PENDING] == frozenset(
        {
            ProcessingLifecycleStatus.RUNNING,
            ProcessingLifecycleStatus.CANCELLED,
        }
    )

    # From RUNNING: only SUCCEEDED, FAILED, and CANCELLED
    assert ALLOWED_PROCESSING_TRANSITIONS[ProcessingLifecycleStatus.RUNNING] == frozenset(
        {
            ProcessingLifecycleStatus.SUCCEEDED,
            ProcessingLifecycleStatus.FAILED,
            ProcessingLifecycleStatus.CANCELLED,
        }
    )

    # Terminal states: empty transition set
    for terminal in TERMINAL_PROCESSING_STATUSES:
        assert ALLOWED_PROCESSING_TRANSITIONS[terminal] == frozenset()


@pytest.mark.parametrize(
    "status",
    [
        ProcessingLifecycleStatus.SUCCEEDED,
        ProcessingLifecycleStatus.FAILED,
        ProcessingLifecycleStatus.CANCELLED,
    ],
)
@pytest.mark.asyncio
async def test_start_execution_terminal_state_raises_terminal_error(
    status: ProcessingLifecycleStatus,
) -> None:
    """Verify starting an execution in any terminal state raises ProcessingTerminalStateError."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=status.value,
        finished_at=datetime.now(UTC),
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    with pytest.raises(ProcessingTerminalStateError, match="terminal state"):
        await ProcessingLifecycleService.start_execution(session, processing_id=record.id)


@pytest.mark.asyncio
async def test_start_execution_non_pending_raises_invalid_transition() -> None:
    """Verify starting an execution already in 'running' raises InvalidProcessingTransitionError."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.RUNNING.value,
        started_at=datetime.now(UTC),
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    with pytest.raises(InvalidProcessingTransitionError, match="Cannot transition"):
        await ProcessingLifecycleService.start_execution(session, processing_id=record.id)


@pytest.mark.asyncio
async def test_complete_execution_non_running_raises() -> None:
    """Verify completing an execution in 'pending' raises InvalidProcessingTransitionError."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.PENDING.value,
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    with pytest.raises(InvalidProcessingTransitionError, match="Cannot transition"):
        await ProcessingLifecycleService.complete_execution(
            session,
            processing_id=record.id,
            result={"status": "success"},
        )


@pytest.mark.asyncio
async def test_complete_execution_validates_json_serializability() -> None:
    """Verify complete_execution validates JSON compatibility using Phase 4.1 rules."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.RUNNING.value,
        started_at=datetime.now(UTC),
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    # Non-serializable object
    with pytest.raises(ProcessingValidationError, match="JSON validation"):
        await ProcessingLifecycleService.complete_execution(
            session,
            processing_id=record.id,
            result={"bad_key": object()},
        )

    # NaN float
    with pytest.raises(ProcessingValidationError, match="Non-finite float"):
        await ProcessingLifecycleService.complete_execution(
            session,
            processing_id=record.id,
            result={"invalid_number": float("nan")},
        )

    # Circular dictionary
    cyclic_dict: dict[str, object] = {"a": 1}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(ProcessingValidationError, match="Circular reference detected"):
        await ProcessingLifecycleService.complete_execution(
            session,
            processing_id=record.id,
            result=cyclic_dict,
        )


@pytest.mark.asyncio
async def test_complete_execution_accepts_valid_processing_result() -> None:
    """Verify complete_execution accepts a strongly-typed ProcessingResult instance."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="pdf-proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.RUNNING.value,
        started_at=datetime.now(UTC),
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    result = ProcessingResult(
        processor_name="pdf-proc",
        processor_version="1.0.0",
        extracted_text="Extracted plain text sample",
        metadata={"total_pages": 5, "author": "Alice"},
    )

    updated = await ProcessingLifecycleService.complete_execution(
        session,
        processing_id=record.id,
        result=result,
    )

    assert updated.status == ProcessingLifecycleStatus.SUCCEEDED.value
    assert updated.result is not None
    assert updated.result["extracted_text"] == "Extracted plain text sample"
    assert updated.result_schema_version == "1.0.0"
    assert updated.finished_at is not None


@pytest.mark.asyncio
async def test_fail_execution_sanitizes_filesystem_paths() -> None:
    """Verify internal filesystem paths are scrubbed from persisted error messages."""
    session = AsyncMock()
    record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.RUNNING.value,
        started_at=datetime.now(UTC),
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = record
    session.execute.return_value = mock_res

    leaky_msg = "Error reading temp file at C:\\docker\\OCR\\storage_data\\secret.bin on host"
    failed = await ProcessingLifecycleService.fail_execution(
        session,
        processing_id=record.id,
        error_code="INTERNAL_IO_ERROR",
        error_message=leaky_msg,
    )

    assert failed.status == ProcessingLifecycleStatus.FAILED.value
    assert failed.error_code == "INTERNAL_IO_ERROR"
    assert "C:\\" not in (failed.error_message or "")
    assert "secret.bin" not in (failed.error_message or "")
    assert "[redacted_path]" in (failed.error_message or "")
    assert failed.finished_at is not None


@pytest.mark.asyncio
async def test_cancel_execution_semantics() -> None:
    """Verify cancellation semantics:
    Allowed from pending/running, idempotent for cancelled, forbidden for terminal.
    """
    session = AsyncMock()

    # 1. Cancel from pending
    pending_record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.PENDING.value,
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = pending_record
    session.execute.return_value = mock_res

    cancelled = await ProcessingLifecycleService.cancel_execution(
        session,
        processing_id=pending_record.id,
        reason="Aborted by user in /tmp/scratch/job.txt",
    )
    assert cancelled.status == ProcessingLifecycleStatus.CANCELLED.value
    assert cancelled.finished_at is not None
    assert "/tmp/scratch" not in (cancelled.error_message or "")
    assert "[redacted_path]" in (cancelled.error_message or "")

    # 2. Idempotent re-cancellation
    same = await ProcessingLifecycleService.cancel_execution(
        session,
        processing_id=pending_record.id,
    )
    assert same.status == ProcessingLifecycleStatus.CANCELLED.value

    # 3. Cancel terminal succeeded execution raises
    succeeded_record = DocumentProcessing(
        document_id=uuid.uuid4(),
        processor_name="proc",
        processor_version="1.0.0",
        input_sha256=SAMPLE_SHA256,
        status=ProcessingLifecycleStatus.SUCCEEDED.value,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    mock_res.scalar_one_or_none.return_value = succeeded_record
    with pytest.raises(ProcessingTerminalStateError, match="terminal state"):
        await ProcessingLifecycleService.cancel_execution(
            session,
            processing_id=succeeded_record.id,
        )


@pytest.mark.parametrize(
    ("limit", "offset"),
    [
        (0, 0),  # limit < 1
        (101, 0),  # limit > 100
        (20, -1),  # offset < 0
    ],
)
@pytest.mark.asyncio
async def test_list_executions_pagination_validation(limit: int, offset: int) -> None:
    """Verify pagination validation boundaries (1 <= limit <= 100, offset >= 0)."""
    session = AsyncMock()
    with pytest.raises(ProcessingValidationError):
        await ProcessingLifecycleService.list_executions_for_document(
            session,
            document_id=uuid.uuid4(),
            limit=limit,
            offset=offset,
        )
