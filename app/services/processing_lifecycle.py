"""Domain persistence service managing document processing lifecycle and state machine."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import Document
from app.db.models.document_processing import (
    ALLOWED_PROCESSING_TRANSITIONS,
    TERMINAL_PROCESSING_STATUSES,
    DocumentProcessing,
    ProcessingLifecycleStatus,
)
from app.processing.exceptions import _sanitize_message
from app.processing.result import ProcessingResult, _check_json_serializable

logger = logging.getLogger(__name__)

_PROCESSOR_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9._-]+)?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ProcessingLifecycleError(Exception):
    """Base exception for processing lifecycle and persistence operations."""


class DocumentNotFoundError(ProcessingLifecycleError):
    """Raised when the target document does not exist in PostgreSQL."""


class DocumentPendingDeletionError(ProcessingLifecycleError):
    """Raised when attempting to create a processing execution for a document pending deletion."""


class ProcessingExecutionNotFoundError(ProcessingLifecycleError):
    """Raised when a specific processing execution record is not found."""


class InvalidProcessingTransitionError(ProcessingLifecycleError):
    """Raised when a lifecycle status transition violates the state machine rules."""


class ProcessingTerminalStateError(InvalidProcessingTransitionError):
    """Raised when an operation attempts to modify a processing record in a terminal state."""


ProcessingAlreadyCompletedError = ProcessingTerminalStateError


class ProcessingValidationError(ProcessingLifecycleError):
    """Raised when processing input parameters or result payloads fail validation."""


class ProcessingConcurrencyError(ProcessingLifecycleError):
    """Raised when a concurrent transaction conflicts with the processing state transition."""


def _validate_processor_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ProcessingValidationError("processor_name must be a non-empty string.")
    if len(name) > 64:
        raise ProcessingValidationError("processor_name exceeds maximum length of 64 characters.")
    if not _PROCESSOR_NAME_PATTERN.match(name.strip()):
        raise ProcessingValidationError(
            f"processor_name '{name}' contains invalid characters "
            "(must be alphanumeric, hyphens, or underscores)."
        )


def _validate_processor_version(version: str) -> None:
    if not isinstance(version, str) or not version.strip():
        raise ProcessingValidationError("processor_version must be a non-empty string.")
    if len(version) > 32:
        raise ProcessingValidationError(
            "processor_version exceeds maximum length of 32 characters."
        )
    if not _VERSION_PATTERN.match(version.strip()):
        raise ProcessingValidationError(
            f"processor_version '{version}' is not valid semantic versioning (expected X.Y.Z)."
        )


def _validate_sha256(sha256: str) -> None:
    if not isinstance(sha256, str) or not _SHA256_PATTERN.match(sha256.strip()):
        raise ProcessingValidationError(
            "input_sha256 must be a 64-character lowercase hexadecimal string."
        )


class ProcessingLifecycleService:
    """Service managing DocumentProcessing persistence, state transitions, and retrieval."""

    @staticmethod
    async def create_execution(
        session: AsyncSession,
        *,
        document_id: uuid.UUID,
        processor_name: str,
        processor_version: str,
        input_sha256: str,
    ) -> DocumentProcessing:
        """Create a new processing execution in 'pending' status.

        Validates:
        - Target document exists.
        - Target document is not marked as pending deletion (`deletion_pending_at is None`).
        - Processor identity and checksum formats are valid.
        """
        _validate_processor_name(processor_name)
        _validate_processor_version(processor_version)
        _validate_sha256(input_sha256)

        stmt = select(Document).where(Document.id == document_id).with_for_update()
        res = await session.execute(stmt)
        doc = res.scalar_one_or_none()

        if doc is None:
            raise DocumentNotFoundError(f"Document '{document_id}' not found.")

        if doc.deletion_pending_at is not None:
            raise DocumentPendingDeletionError(
                f"Cannot initiate processing for document '{document_id}' "
                "because it is pending deletion."
            )

        now = datetime.now(UTC)
        execution = DocumentProcessing(
            document_id=document_id,
            processor_name=processor_name.strip(),
            processor_version=processor_version.strip(),
            input_sha256=input_sha256.strip().lower(),
            status=ProcessingLifecycleStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        await session.flush()
        logger.info("Created processing execution %s for document %s", execution.id, document_id)
        return execution

    @staticmethod
    async def start_execution(
        session: AsyncSession,
        execution_id: uuid.UUID | None = None,
        *,
        processing_id: uuid.UUID | None = None,
    ) -> DocumentProcessing:
        """Transition execution from 'pending' to 'running' with row-level lock.

        Sets `started_at` timestamp.
        Rejects invalid transitions or attempts to restart terminal executions.
        """
        target_id = execution_id or processing_id
        if target_id is None:
            raise ProcessingValidationError("execution_id or processing_id is required.")

        stmt = (
            select(DocumentProcessing).where(DocumentProcessing.id == target_id).with_for_update()
        )
        res = await session.execute(stmt)
        execution = res.scalar_one_or_none()

        if execution is None:
            raise ProcessingExecutionNotFoundError(f"Processing execution '{target_id}' not found.")

        current_status = ProcessingLifecycleStatus(execution.status)
        if current_status in TERMINAL_PROCESSING_STATUSES:
            raise ProcessingTerminalStateError(
                f"Cannot start execution '{target_id}' in terminal state '{current_status.value}'."
            )

        if current_status != ProcessingLifecycleStatus.PENDING:
            raise InvalidProcessingTransitionError(
                f"Cannot transition execution '{target_id}' from "
                f"'{current_status.value}' to 'running'."
            )

        now = datetime.now(UTC)
        execution.status = ProcessingLifecycleStatus.RUNNING.value
        execution.started_at = now
        execution.updated_at = now
        await session.flush()
        logger.info("Started processing execution %s", target_id)
        return execution

    @staticmethod
    async def complete_execution(
        session: AsyncSession,
        execution_id: uuid.UUID | None = None,
        result: ProcessingResult | dict[str, Any] | None = None,
        *,
        processing_id: uuid.UUID | None = None,
    ) -> DocumentProcessing:
        """Transition execution from 'running' to 'succeeded' with validated structured result.

        Sets `result`, `result_schema_version`, and `finished_at`.
        Rejects non-JSON serializable results, NaN/Inf, or cyclic structures.
        """
        target_id = execution_id or processing_id
        if target_id is None:
            raise ProcessingValidationError("execution_id or processing_id is required.")
        if result is None:
            raise ProcessingValidationError("result payload is required.")

        stmt = (
            select(DocumentProcessing).where(DocumentProcessing.id == target_id).with_for_update()
        )
        res = await session.execute(stmt)
        execution = res.scalar_one_or_none()

        if execution is None:
            raise ProcessingExecutionNotFoundError(f"Processing execution '{target_id}' not found.")

        current_status = ProcessingLifecycleStatus(execution.status)
        if current_status in TERMINAL_PROCESSING_STATUSES:
            raise ProcessingTerminalStateError(
                f"Cannot complete execution '{target_id}' in "
                f"terminal state '{current_status.value}'."
            )

        if current_status != ProcessingLifecycleStatus.RUNNING:
            raise InvalidProcessingTransitionError(
                f"Cannot transition execution '{target_id}' from "
                f"'{current_status.value}' to 'succeeded'."
            )

        if isinstance(result, ProcessingResult):
            dict_result = result.to_dict()
            schema_version = str(result.schema_version)
        elif isinstance(result, dict):
            dict_result = result
            schema_version = str(result.get("schema_version", "1.0.0"))
        else:
            raise ProcessingValidationError(
                f"Expected ProcessingResult or dict, got {type(result).__name__}."
            )

        # Validate JSON compatibility using Phase 4.1 strict rules
        try:
            _check_json_serializable(dict_result)
        except ValueError as exc:
            raise ProcessingValidationError(
                f"Processing result failed JSON validation: {exc}"
            ) from exc

        now = datetime.now(UTC)
        execution.status = ProcessingLifecycleStatus.SUCCEEDED.value
        execution.result = dict_result
        execution.result_schema_version = schema_version
        execution.finished_at = now
        execution.updated_at = now
        await session.flush()
        logger.info("Successfully completed processing execution %s", target_id)
        return execution

    @staticmethod
    async def fail_execution(
        session: AsyncSession,
        execution_id: uuid.UUID | None = None,
        error_code: str = "PROCESSING_FAILED",
        error_message: str = "",
        *,
        processing_id: uuid.UUID | None = None,
    ) -> DocumentProcessing:
        """Transition execution from 'running' to 'failed' with sanitized error details.

        Sets `error_code`, sanitized `error_message`, and `finished_at`.
        Scans and redacts potential internal filesystem paths from the error message.
        """
        target_id = execution_id or processing_id
        if target_id is None:
            raise ProcessingValidationError("execution_id or processing_id is required.")

        stmt = (
            select(DocumentProcessing).where(DocumentProcessing.id == target_id).with_for_update()
        )
        res = await session.execute(stmt)
        execution = res.scalar_one_or_none()

        if execution is None:
            raise ProcessingExecutionNotFoundError(f"Processing execution '{target_id}' not found.")

        current_status = ProcessingLifecycleStatus(execution.status)
        if current_status in TERMINAL_PROCESSING_STATUSES:
            raise ProcessingTerminalStateError(
                f"Cannot fail execution '{target_id}' in terminal state '{current_status.value}'."
            )

        if current_status != ProcessingLifecycleStatus.RUNNING:
            raise InvalidProcessingTransitionError(
                f"Cannot transition execution '{target_id}' from "
                f"'{current_status.value}' to 'failed'."
            )

        code_clean = str(error_code).strip()
        if not code_clean:
            code_clean = "PROCESSING_FAILED"
        if len(code_clean) > 64:
            code_clean = code_clean[:64]

        clean_message = _sanitize_message(str(error_message))

        now = datetime.now(UTC)
        execution.status = ProcessingLifecycleStatus.FAILED.value
        execution.error_code = code_clean
        execution.error_message = clean_message
        execution.finished_at = now
        execution.updated_at = now
        await session.flush()
        logger.info(
            "Marked processing execution %s as failed (code=%s)",
            target_id,
            code_clean,
        )
        return execution

    @staticmethod
    async def cancel_execution(
        session: AsyncSession,
        execution_id: uuid.UUID | None = None,
        reason: str | None = None,
        *,
        processing_id: uuid.UUID | None = None,
    ) -> DocumentProcessing:
        """Cancel an active processing execution in 'pending' or 'running' status.

        - Idempotent if already 'cancelled'.
        - Rejects cancellation of 'succeeded' or 'failed' terminal executions.
        - Sets `finished_at` and sanitized cancellation reason in `error_message`.
        """
        target_id = execution_id or processing_id
        if target_id is None:
            raise ProcessingValidationError("execution_id or processing_id is required.")

        stmt = (
            select(DocumentProcessing).where(DocumentProcessing.id == target_id).with_for_update()
        )
        res = await session.execute(stmt)
        execution = res.scalar_one_or_none()

        if execution is None:
            raise ProcessingExecutionNotFoundError(f"Processing execution '{target_id}' not found.")

        current_status = ProcessingLifecycleStatus(execution.status)

        # Idempotent: already cancelled
        if current_status == ProcessingLifecycleStatus.CANCELLED:
            return execution

        if current_status in (
            ProcessingLifecycleStatus.SUCCEEDED,
            ProcessingLifecycleStatus.FAILED,
        ):
            raise ProcessingTerminalStateError(
                f"Cannot cancel execution '{target_id}' in terminal state '{current_status.value}'."
            )

        allowed = ALLOWED_PROCESSING_TRANSITIONS.get(current_status, frozenset())
        if ProcessingLifecycleStatus.CANCELLED not in allowed:
            raise InvalidProcessingTransitionError(
                f"Cannot transition execution '{target_id}' from "
                f"'{current_status.value}' to 'cancelled'."
            )

        now = datetime.now(UTC)
        execution.status = ProcessingLifecycleStatus.CANCELLED.value
        execution.finished_at = now
        execution.updated_at = now
        if reason:
            execution.error_message = _sanitize_message(str(reason))

        await session.flush()
        logger.info("Cancelled processing execution %s", target_id)
        return execution

    @staticmethod
    async def get_execution(
        session: AsyncSession,
        execution_id: uuid.UUID | None = None,
        *,
        processing_id: uuid.UUID | None = None,
    ) -> DocumentProcessing | None:
        """Retrieve a single processing execution by its unique UUID identifier."""
        target_id = execution_id or processing_id
        if target_id is None:
            raise ProcessingValidationError("execution_id or processing_id is required.")
        stmt = select(DocumentProcessing).where(DocumentProcessing.id == target_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_executions_for_document(
        session: AsyncSession,
        *,
        document_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
        status: ProcessingLifecycleStatus | str | None = None,
    ) -> tuple[list[DocumentProcessing], int]:
        """Retrieve paginated processing history for a document with deterministic sorting.

        - Bounded pagination: 1 <= limit <= 100, offset >= 0.
        - Deterministic secondary sorting on `id` prevents pagination jitter.
        - Optional status filtering.
        - Returns (items, total_count).
        """
        if limit < 1 or limit > 100:
            raise ProcessingValidationError("limit must be between 1 and 100.")
        if offset < 0:
            raise ProcessingValidationError("offset must be a non-negative integer.")

        conditions = [DocumentProcessing.document_id == document_id]

        if status is not None:
            raw_status = (
                status.value if isinstance(status, ProcessingLifecycleStatus) else str(status)
            )
            conditions.append(DocumentProcessing.status == raw_status.strip().lower())

        count_stmt = select(func.count()).select_from(DocumentProcessing).where(*conditions)
        total_res = await session.execute(count_stmt)
        total = total_res.scalar_one()

        if total == 0:
            return [], 0

        query_stmt = (
            select(DocumentProcessing)
            .where(*conditions)
            .order_by(DocumentProcessing.created_at.desc(), DocumentProcessing.id.desc())
            .limit(limit)
            .offset(offset)
        )
        items_res = await session.execute(query_stmt)
        items = list(items_res.scalars().all())

        return items, total
