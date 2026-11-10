"""Integration tests for Document Processing persistence model and lifecycle against PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.core.config import get_settings
from app.db.models.document import Document
from app.db.models.document_processing import DocumentProcessing, ProcessingLifecycleStatus
from app.db.models.user import User
from app.db.session import close_engine, get_engine, get_session_factory
from app.processing import ProcessingResult
from app.services.document import delete_document
from app.services.processing_lifecycle import (
    DocumentNotFoundError,
    DocumentPendingDeletionError,
    InvalidProcessingTransitionError,
    ProcessingAlreadyCompletedError,
    ProcessingLifecycleService,
)
from app.storage.base import StorageBackend
from tests.integration.test_database import is_postgres_available


def get_alembic_config() -> Config:
    """Create an Alembic Config pointing to project configuration."""
    settings = get_settings()
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return cfg


@pytest.fixture
async def sample_user() -> User:
    """Create and persist an isolated test user."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        user = User(
            email=f"proc_user_{secrets.token_hex(4)}@example.com",
            username=f"proc_user_{secrets.token_hex(4)}",
            password_hash="mock_argon2id_hash_for_processing_lifecycle_test",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def sample_document(sample_user: User) -> Document:
    """Create and persist an isolated test document."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        doc = Document(
            user_id=sample_user.id,
            storage_key=secrets.token_hex(16),
            original_filename="sample_test_doc.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc


@pytest.mark.asyncio
async def test_document_processing_table_schema_and_constraints() -> None:
    """Verify document_processings table structure, types, and constraints in PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'document_processings' "
                "ORDER BY ordinal_position;"
            )
        )
        columns = {row[0]: (row[1], row[2]) for row in res.fetchall()}

        assert "id" in columns
        assert columns["id"] == ("uuid", "NO")

        assert "document_id" in columns
        assert columns["document_id"] == ("uuid", "NO")

        assert "processor_name" in columns
        assert columns["processor_name"] == ("character varying", "NO")

        assert "processor_version" in columns
        assert columns["processor_version"] == ("character varying", "NO")

        assert "status" in columns
        assert columns["status"] == ("character varying", "NO")

        assert "input_sha256" in columns
        assert columns["input_sha256"] == ("character varying", "NO")

        assert "result_schema_version" in columns
        assert columns["result_schema_version"] == ("character varying", "YES")

        assert "result" in columns
        assert columns["result"] == ("jsonb", "YES")

        assert "error_code" in columns
        assert columns["error_code"] == ("character varying", "YES")

        assert "error_message" in columns
        assert columns["error_message"] == ("text", "YES")

        assert "created_at" in columns
        assert columns["created_at"] == ("timestamp with time zone", "NO")

        assert "started_at" in columns
        assert columns["started_at"] == ("timestamp with time zone", "YES")

        assert "finished_at" in columns
        assert columns["finished_at"] == ("timestamp with time zone", "YES")

        assert "updated_at" in columns
        assert columns["updated_at"] == ("timestamp with time zone", "NO")

        # Verify check constraints
        res_ck = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON c.conrelid = t.oid "
                "WHERE t.relname = 'document_processings' AND c.contype = 'c';"
            )
        )
        ck_names = {row[0] for row in res_ck.fetchall()}
        assert "ck_document_processings_status" in ck_names
        assert "ck_document_processings_input_sha256" in ck_names
        assert "ck_document_processings_timestamps" in ck_names
        assert "ck_document_processings_status_fields" in ck_names

        # Verify indexes
        res_idx = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'document_processings';")
        )
        idx_names = {row[0] for row in res_idx.fetchall()}
        assert "pk_document_processings" in idx_names
        assert "ix_document_processings_document_id_created_at" in idx_names
        assert "ix_document_processings_status_created_at" in idx_names
        assert "ix_document_processings_document_id_status" in idx_names


@pytest.mark.asyncio
async def test_create_processing_execution_db(sample_document: Document) -> None:
    """Verify persisting a pending processing execution to PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="tesseract_ocr",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        assert isinstance(execution.id, uuid.UUID)
        assert execution.document_id == sample_document.id
        assert execution.processor_name == "tesseract_ocr"
        assert execution.processor_version == "1.0.0"
        assert execution.status == ProcessingLifecycleStatus.PENDING
        assert execution.input_sha256 == sample_document.sha256
        assert execution.started_at is None
        assert execution.finished_at is None
        assert execution.result is None
        assert execution.result_schema_version is None
        assert execution.error_code is None
        assert execution.error_message is None
        assert execution.created_at is not None
        assert execution.updated_at is not None

        # Verify persisted via raw query
        res = await session.execute(
            select(DocumentProcessing).where(DocumentProcessing.id == execution.id)
        )
        fetched = res.scalar_one()
        assert fetched.id == execution.id
        assert fetched.status == ProcessingLifecycleStatus.PENDING


@pytest.mark.asyncio
async def test_start_execution_db(sample_document: Document) -> None:
    """Verify pending -> running transition persists started_at and status in PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="pdf_extractor",
            processor_version="2.1.0",
            input_sha256=sample_document.sha256,
        )
        execution_id = execution.id
        await session.commit()

    async with session_factory() as session:
        started = await service.start_execution(session=session, execution_id=execution_id)
        assert started.status == ProcessingLifecycleStatus.RUNNING
        assert started.started_at is not None
        assert started.finished_at is None
        await session.commit()

    # Verify query in a fresh session
    async with session_factory() as session:
        fetched = await service.get_execution(session=session, execution_id=execution_id)
        assert fetched is not None
        assert fetched.status == ProcessingLifecycleStatus.RUNNING
        assert fetched.started_at is not None
        assert fetched.finished_at is None


@pytest.mark.asyncio
async def test_complete_execution_db(sample_document: Document) -> None:
    """Verify running -> succeeded transition persists JSONB result and computes duration."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="pdf_extractor",
            processor_version="2.1.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=execution.id)
        await session.commit()
        execution_id = execution.id

    result_payload = ProcessingResult(
        processor_name="pdf_extractor",
        processor_version="2.1.0",
        schema_version="1.0.0",
        extracted_text="Extracted document body.",
        metadata={"confidence": 0.99, "engine": "pdf_v2", "language": "eng"},
    )

    async with session_factory() as session:
        completed = await service.complete_execution(
            session=session,
            execution_id=execution_id,
            result=result_payload,
        )
        assert completed.status == ProcessingLifecycleStatus.SUCCEEDED
        assert completed.finished_at is not None
        assert completed.result_schema_version == "1.0.0"
        assert completed.result == result_payload.to_dict()
        assert completed.error_code is None
        assert completed.error_message is None
        assert completed.duration_seconds is not None
        assert completed.duration_seconds >= 0.0
        await session.commit()

    # Verify JSONB query in PostgreSQL
    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT result->'metadata'->>'language', result_schema_version "
                "FROM document_processings WHERE id = :id"
            ),
            {"id": execution_id},
        )
        row = res.fetchone()
        assert row is not None
        assert row[0] == "eng"
        assert row[1] == "1.0.0"


@pytest.mark.asyncio
async def test_fail_execution_db(sample_document: Document) -> None:
    """Verify running -> failed transition persists error code and sanitized error message."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="scanner",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=execution.id)
        await session.commit()
        execution_id = execution.id

    raw_error_message = (
        "Failed reading file at /tmp/scratch/input_document.pdf: "
        "permission denied on C:\\docker\\OCR\\data\\private.key"
    )

    async with session_factory() as session:
        failed = await service.fail_execution(
            session=session,
            execution_id=execution_id,
            error_code="EXTRACTION_FAILED",
            error_message=raw_error_message,
        )
        assert failed.status == ProcessingLifecycleStatus.FAILED
        assert failed.finished_at is not None
        assert failed.error_code == "EXTRACTION_FAILED"
        assert failed.result is None
        # Paths must be sanitized
        assert failed.error_message is not None
        assert "/tmp/scratch/input_document.pdf" not in failed.error_message
        assert "C:\\docker\\OCR\\data\\private.key" not in failed.error_message
        assert "[redacted_path]" in failed.error_message
        await session.commit()


@pytest.mark.asyncio
async def test_cancel_execution_from_pending_db(sample_document: Document) -> None:
    """Verify pending -> cancelled transition persists finished_at with started_at NULL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="scanner",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await session.commit()
        execution_id = execution.id

    async with session_factory() as session:
        cancelled = await service.cancel_execution(
            session=session,
            execution_id=execution_id,
            reason="Aborted by user request before queue dispatch",
        )
        assert cancelled.status == ProcessingLifecycleStatus.CANCELLED
        assert cancelled.started_at is None
        assert cancelled.finished_at is not None
        assert cancelled.error_message == "Aborted by user request before queue dispatch"
        await session.commit()


@pytest.mark.asyncio
async def test_cancel_execution_from_running_db(sample_document: Document) -> None:
    """Verify running -> cancelled transition preserves started_at and sets finished_at."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="scanner",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=execution.id)
        await session.commit()
        execution_id = execution.id

    async with session_factory() as session:
        cancelled = await service.cancel_execution(
            session=session,
            execution_id=execution_id,
            reason="Cancelled during execution due to timeout",
        )
        assert cancelled.status == ProcessingLifecycleStatus.CANCELLED
        assert cancelled.started_at is not None
        assert cancelled.finished_at is not None
        assert cancelled.duration_seconds is not None
        assert cancelled.duration_seconds >= 0.0
        await session.commit()


@pytest.mark.asyncio
async def test_cancel_terminal_execution_rejected_db(sample_document: Document) -> None:
    """Verify that cancellation of terminal executions raises ProcessingAlreadyCompletedError."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        exec_succeeded = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="ocr",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=exec_succeeded.id)
        await service.complete_execution(
            session=session,
            execution_id=exec_succeeded.id,
            result={"text": "hello"},
        )

        exec_failed = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="ocr",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=exec_failed.id)
        await service.fail_execution(
            session=session,
            execution_id=exec_failed.id,
            error_code="ERR_FAIL",
            error_message="failed",
        )

        exec_cancelled = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="ocr",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.cancel_execution(
            session=session,
            execution_id=exec_cancelled.id,
            reason="cancelled",
        )
        await session.commit()

        # Attempt to cancel succeeded execution -> rejected
        with pytest.raises(ProcessingAlreadyCompletedError) as exc_info:
            await service.cancel_execution(
                session=session,
                execution_id=exec_succeeded.id,
                reason="retry",
            )
        assert "succeeded" in str(exc_info.value)

        # Attempt to cancel failed execution -> rejected
        with pytest.raises(ProcessingAlreadyCompletedError) as exc_info:
            await service.cancel_execution(
                session=session,
                execution_id=exec_failed.id,
                reason="retry",
            )
        assert "failed" in str(exc_info.value)

        # Cancelling an already cancelled execution is an idempotent no-op
        idempotent_cancelled = await service.cancel_execution(
            session=session,
            execution_id=exec_cancelled.id,
            reason="retry",
        )
        assert idempotent_cancelled.status == ProcessingLifecycleStatus.CANCELLED


@pytest.mark.asyncio
async def test_multiple_executions_for_one_document_db(sample_document: Document) -> None:
    """Verify multiple executions can exist for the same document."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        # Attempt 1: Failed
        attempt1 = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="engine_a",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=attempt1.id)
        await service.fail_execution(
            session=session,
            execution_id=attempt1.id,
            error_code="TIMEOUT",
            error_message="Process timed out",
        )

        # Attempt 2: Succeeded
        attempt2 = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="engine_b",
            processor_version="2.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=attempt2.id)
        await service.complete_execution(
            session=session,
            execution_id=attempt2.id,
            result={"output": "success"},
        )

        assert attempt1.id != attempt2.id

        # Query all executions for document
        items, total = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
        )
        assert total == 2
        assert len(items) == 2
        # Deterministic order (created_at DESC, id DESC) -> attempt2 first
        assert items[0].id == attempt2.id
        assert items[0].status == ProcessingLifecycleStatus.SUCCEEDED
        assert items[1].id == attempt1.id
        assert items[1].status == ProcessingLifecycleStatus.FAILED


@pytest.mark.asyncio
async def test_history_pagination_and_status_filtering_db(sample_document: Document) -> None:
    """Verify history pagination, offset, status filter, and total count."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        # Create 5 executions: 3 succeeded, 2 failed
        created_ids = []
        for i in range(5):
            rec = await service.create_execution(
                session=session,
                document_id=sample_document.id,
                processor_name="multi_proc",
                processor_version=f"1.0.{i}",
                input_sha256=sample_document.sha256,
            )
            await service.start_execution(session=session, execution_id=rec.id)
            if i % 2 == 0:
                await service.complete_execution(
                    session=session,
                    execution_id=rec.id,
                    result={"run": i},
                )
            else:
                await service.fail_execution(
                    session=session,
                    execution_id=rec.id,
                    error_code="RUN_FAILED",
                    error_message=f"Run {i} failed",
                )
            created_ids.append(rec.id)

        # 1. Total count
        all_items, total = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
            limit=10,
            offset=0,
        )
        assert total == 5
        assert len(all_items) == 5

        # 2. Pagination: limit=2, offset=0
        page1, total1 = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
            limit=2,
            offset=0,
        )
        assert total1 == 5
        assert len(page1) == 2

        # 3. Pagination: limit=2, offset=2
        page2, total2 = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
            limit=2,
            offset=2,
        )
        assert total2 == 5
        assert len(page2) == 2
        # No overlap between page 1 and page 2
        p1_ids = {p.id for p in page1}
        p2_ids = {p.id for p in page2}
        assert p1_ids.isdisjoint(p2_ids)

        # 4. Status filter: SUCCEEDED
        succeeded_items, succ_total = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
            status=ProcessingLifecycleStatus.SUCCEEDED,
        )
        assert succ_total == 3
        assert len(succeeded_items) == 3
        for item in succeeded_items:
            assert item.status == ProcessingLifecycleStatus.SUCCEEDED

        # 5. Status filter: FAILED
        failed_items, fail_total = await service.list_executions_for_document(
            session=session,
            document_id=sample_document.id,
            status=ProcessingLifecycleStatus.FAILED,
        )
        assert fail_total == 2
        assert len(failed_items) == 2
        for item in failed_items:
            assert item.status == ProcessingLifecycleStatus.FAILED


@pytest.mark.asyncio
async def test_create_execution_nonexistent_document_raises_db() -> None:
    """Verify creating execution for non-existent document ID raises DocumentNotFoundError."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        random_doc_id = uuid.uuid4()
        with pytest.raises(DocumentNotFoundError) as exc_info:
            await service.create_execution(
                session=session,
                document_id=random_doc_id,
                processor_name="proc",
                processor_version="1.0.0",
                input_sha256="a" * 64,
            )
        assert str(random_doc_id) in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_execution_document_pending_deletion_raises_db(
    sample_document: Document,
) -> None:
    """Verify document with deletion_pending_at set cannot have new processing executions."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    # Mark document as deletion pending
    async with session_factory() as session:
        doc = await session.get(Document, sample_document.id)
        assert doc is not None
        doc.deletion_pending_at = datetime.now(UTC)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(DocumentPendingDeletionError) as exc_info:
            await service.create_execution(
                session=session,
                document_id=sample_document.id,
                processor_name="proc",
                processor_version="1.0.0",
                input_sha256=sample_document.sha256,
            )
        assert str(sample_document.id) in str(exc_info.value)


@pytest.mark.asyncio
async def test_document_deletion_cascades_to_processing_runs_db(
    sample_document: Document,
) -> None:
    """Verify physical deletion of document cascades to remove document_processings rows."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    # Create 2 processing runs
    async with session_factory() as session:
        p1 = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="p1",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        p2 = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="p2",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        p1_id, p2_id = p1.id, p2.id

    # Physically delete the document
    async with session_factory() as session:
        await session.execute(delete(Document).where(Document.id == sample_document.id))
        await session.commit()

    # Verify both processing records were cascaded and deleted by PostgreSQL
    async with session_factory() as session:
        res = await session.execute(
            select(DocumentProcessing).where(DocumentProcessing.id.in_([p1_id, p2_id]))
        )
        remaining = res.scalars().all()
        assert len(remaining) == 0


@pytest.mark.asyncio
async def test_transaction_rollback_leaves_no_partial_updates_db(
    sample_document: Document,
) -> None:
    """Verify transaction rollback leaves execution in its previous consistent state."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="proc",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await session.commit()
        execution_id = execution.id

    # Start execution in a session, but rollback instead of commit
    async with session_factory() as session:
        rec = await session.get(DocumentProcessing, execution_id)
        assert rec is not None
        rec.status = ProcessingLifecycleStatus.RUNNING.value
        rec.started_at = datetime.now(UTC)
        await session.rollback()

    # Fresh session reads back status
    async with session_factory() as session:
        fetched = await service.get_execution(session=session, execution_id=execution_id)
        assert fetched is not None
        assert fetched.status == ProcessingLifecycleStatus.PENDING
        assert fetched.started_at is None


@pytest.mark.asyncio
async def test_concurrent_start_attempts_db(sample_document: Document) -> None:
    """Verify concurrent start attempts are serialized; one succeeds and one raises error."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="concurrent_proc",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await session.commit()
        execution_id = execution.id

    async def attempt_start() -> str:
        async with session_factory() as session:
            try:
                await service.start_execution(session=session, execution_id=execution_id)
                await session.commit()
                return "started"
            except InvalidProcessingTransitionError:
                await session.rollback()
                return "rejected"

    results = await asyncio.gather(attempt_start(), attempt_start())
    assert sorted(results) == ["rejected", "started"]

    # Verify final DB state is RUNNING
    async with session_factory() as session:
        final_state = await service.get_execution(session=session, execution_id=execution_id)
        assert final_state is not None
        assert final_state.status == ProcessingLifecycleStatus.RUNNING


@pytest.mark.asyncio
async def test_concurrent_terminal_transitions_db(sample_document: Document) -> None:
    """Verify concurrent terminal transitions: one succeeds and the competitor is rejected."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="terminal_race",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=execution.id)
        await session.commit()
        execution_id = execution.id

    async def attempt_complete() -> str:
        async with session_factory() as session:
            try:
                await service.complete_execution(
                    session=session,
                    execution_id=execution_id,
                    result={"status": "ok"},
                )
                await session.commit()
                return "completed"
            except ProcessingAlreadyCompletedError:
                await session.rollback()
                return "already_completed"

    async def attempt_fail() -> str:
        async with session_factory() as session:
            try:
                await service.fail_execution(
                    session=session,
                    execution_id=execution_id,
                    error_code="RACE_FAIL",
                    error_message="Competitor failed",
                )
                await session.commit()
                return "failed"
            except ProcessingAlreadyCompletedError:
                await session.rollback()
                return "already_completed"

    results = await asyncio.gather(attempt_complete(), attempt_fail())
    assert "already_completed" in results
    assert "completed" in results or "failed" in results

    # Verify state in DB matches the winner
    async with session_factory() as session:
        final_state = await service.get_execution(session=session, execution_id=execution_id)
        assert final_state is not None
        if "completed" in results:
            assert final_state.status == ProcessingLifecycleStatus.SUCCEEDED
            assert final_state.result == {"status": "ok"}
        else:
            assert final_state.status == ProcessingLifecycleStatus.FAILED
            assert final_state.error_code == "RACE_FAIL"


@pytest.mark.asyncio
async def test_terminal_state_immutability_in_db(sample_document: Document) -> None:
    """Verify that records in terminal state cannot transition and violate check constraints."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    async with session_factory() as session:
        execution = await service.create_execution(
            session=session,
            document_id=sample_document.id,
            processor_name="proc",
            processor_version="1.0.0",
            input_sha256=sample_document.sha256,
        )
        await service.start_execution(session=session, execution_id=execution.id)
        await service.complete_execution(
            session=session,
            execution_id=execution.id,
            result={"val": 42},
        )
        await session.commit()
        execution_id = execution.id

    # 1. Domain service blocks terminal modification
    async with session_factory() as session:
        with pytest.raises(ProcessingAlreadyCompletedError):
            await service.fail_execution(
                session=session,
                execution_id=execution_id,
                error_code="ERR",
                error_message="fail",
            )
        with pytest.raises(ProcessingAlreadyCompletedError):
            await service.start_execution(session=session, execution_id=execution_id)

    # 2. Database check constraint enforces status-to-field consistency
    engine = get_engine()
    async with engine.connect() as conn:
        # Inserting a succeeded record with result IS NULL must violate constraint
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, result, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'succeeded', "
                    "  :sha, NULL, now(), now()"
                    ")"
                ),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "sha": "a" * 64,
                },
            )


@pytest.mark.asyncio
async def test_migration_0012_upgrade_downgrade_reupgrade_cycle() -> None:
    """Verify Alembic downgrade to 0011 and re-upgrade to 0012 on PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    cfg = get_alembic_config()

    await close_engine()

    # Downgrade to 0011
    await asyncio.to_thread(command.downgrade, cfg, "0011_doc_deletion_pending_at")

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            # Verify document_processings table was dropped
            res = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'document_processings'"
                    ");"
                )
            )
            assert res.scalar() is False

            # Verify documents table still exists
            res_doc = await conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'documents'"
                    ");"
                )
            )
            assert res_doc.scalar() is True
        await close_engine()
    finally:
        # Re-upgrade to head
        await asyncio.to_thread(command.upgrade, cfg, "head")

    # Verify table and indexes are restored
    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT FROM information_schema.tables "
                "  WHERE table_name = 'document_processings'"
                ");"
            )
        )
        assert res.scalar() is True


@pytest.mark.asyncio
async def test_concurrent_create_execution_vs_deletion_creation_wins_db(
    sample_document: Document,
) -> None:
    """Verify Scenario A: create_execution locks document first, deletion waits and runs after."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    event_creation_locked = asyncio.Event()
    order_log: list[str] = []

    async def run_creation() -> uuid.UUID:
        async with session_factory() as session:
            # Acquire lock on document as create_execution does
            stmt = select(Document).where(Document.id == sample_document.id).with_for_update()
            res = await session.execute(stmt)
            doc = res.scalar_one()
            assert doc.deletion_pending_at is None
            order_log.append("creation_locked")
            event_creation_locked.set()

            # Small yield to let deletion task invoke execute and block on PostgreSQL lock
            await asyncio.sleep(0.05)

            # Create execution in same transaction
            execution = await service.create_execution(
                session=session,
                document_id=sample_document.id,
                processor_name="ocr_engine",
                processor_version="1.0.0",
                input_sha256=sample_document.sha256,
            )
            order_log.append("creation_created_execution")
            order_log.append("creation_before_commit")
            await session.commit()
            order_log.append("creation_committed")
            return execution.id

    async def run_deletion() -> None:
        await event_creation_locked.wait()
        async with session_factory() as session:
            order_log.append("deletion_attempting_lock")
            stmt = select(Document).where(Document.id == sample_document.id).with_for_update()
            res = await session.execute(stmt)
            doc = res.scalar_one()
            order_log.append("deletion_acquired_lock")
            doc.deletion_pending_at = datetime.now(UTC)
            await session.commit()
            order_log.append("deletion_committed")

    exec_id, _ = await asyncio.gather(run_creation(), run_deletion())

    assert "creation_locked" in order_log
    assert "deletion_attempting_lock" in order_log
    assert "creation_created_execution" in order_log
    assert "creation_before_commit" in order_log
    assert "deletion_acquired_lock" in order_log
    assert "deletion_committed" in order_log
    assert order_log.index("creation_locked") < order_log.index("deletion_attempting_lock")
    assert order_log.index("creation_before_commit") < order_log.index("deletion_acquired_lock")

    async with session_factory() as session:
        exec_record = await service.get_execution(session=session, execution_id=exec_id)
        assert exec_record is not None
        assert exec_record.status == ProcessingLifecycleStatus.PENDING

        doc_record = await session.get(Document, sample_document.id)
        assert doc_record is not None
        assert doc_record.deletion_pending_at is not None
        assert exec_record.created_at <= doc_record.deletion_pending_at


@pytest.mark.asyncio
async def test_concurrent_create_execution_vs_deletion_deletion_wins_db(
    sample_document: Document,
) -> None:
    """Verify Scenario B: deletion locks document first, create_execution waits and raises error."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()

    event_deletion_locked = asyncio.Event()
    order_log: list[str] = []

    async def run_deletion() -> None:
        async with session_factory() as session:
            stmt = select(Document).where(Document.id == sample_document.id).with_for_update()
            res = await session.execute(stmt)
            doc = res.scalar_one()
            order_log.append("deletion_locked")
            doc.deletion_pending_at = datetime.now(UTC)
            event_deletion_locked.set()

            # Hold lock briefly to ensure create_execution initiates its select with_for_update
            await asyncio.sleep(0.05)

            await session.commit()
            order_log.append("deletion_committed")

    async def run_creation() -> str:
        await event_deletion_locked.wait()
        async with session_factory() as session:
            order_log.append("creation_attempting_lock")
            try:
                await service.create_execution(
                    session=session,
                    document_id=sample_document.id,
                    processor_name="ocr_engine",
                    processor_version="1.0.0",
                    input_sha256=sample_document.sha256,
                )
                await session.commit()
                order_log.append("creation_succeeded")
                return "succeeded"
            except DocumentPendingDeletionError:
                await session.rollback()
                order_log.append("creation_rejected_pending_deletion")
                return "rejected"

    _, result = await asyncio.gather(run_deletion(), run_creation())

    assert result == "rejected"
    assert order_log == [
        "deletion_locked",
        "creation_attempting_lock",
        "deletion_committed",
        "creation_rejected_pending_deletion",
    ]

    # Verify no document_processings row was created
    async with session_factory() as session:
        res = await session.execute(
            select(DocumentProcessing).where(DocumentProcessing.document_id == sample_document.id)
        )
        runs = res.scalars().all()
        assert len(runs) == 0


@pytest.mark.asyncio
async def test_concurrent_create_execution_vs_real_delete_document_creation_wins_db(
    sample_document: Document,
) -> None:
    """Verify Scenario A with actual delete_document:

    create_execution locks first, delete_document blocks and cascades after.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()
    mock_storage = AsyncMock(spec=StorageBackend)
    mock_storage.delete.return_value = True

    event_creation_locked = asyncio.Event()
    order_log: list[str] = []

    async def run_creation() -> uuid.UUID:
        async with session_factory() as session:
            stmt = select(Document).where(Document.id == sample_document.id).with_for_update()
            res = await session.execute(stmt)
            doc = res.scalar_one()
            assert doc.deletion_pending_at is None
            order_log.append("creation_locked")
            event_creation_locked.set()

            # Yield to let delete_document invoke select with_for_update and block in PostgreSQL
            await asyncio.sleep(0.05)

            execution = await service.create_execution(
                session=session,
                document_id=sample_document.id,
                processor_name="ocr_engine",
                processor_version="1.0.0",
                input_sha256=sample_document.sha256,
            )
            order_log.append("creation_created_execution")
            await session.commit()
            order_log.append("creation_committed")
            return execution.id

    async def run_deletion() -> None:
        await event_creation_locked.wait()
        async with session_factory() as session:
            order_log.append("deletion_attempting_lock")
            # Calls the real Phase 3.5 delete_document service
            await delete_document(
                document_id=sample_document.id,
                user_id=sample_document.user_id,
                session=session,
                storage=mock_storage,
            )
            order_log.append("deletion_completed")

    exec_id, _ = await asyncio.gather(run_creation(), run_deletion())

    assert "creation_locked" in order_log
    assert "deletion_attempting_lock" in order_log
    assert "creation_created_execution" in order_log
    assert "deletion_completed" in order_log
    assert order_log.index("creation_locked") < order_log.index("deletion_attempting_lock")
    assert order_log.index("creation_created_execution") < order_log.index("deletion_completed")

    # Verify document and execution were cleanly cascade-deleted by delete_document
    async with session_factory() as session:
        doc = await session.get(Document, sample_document.id)
        assert doc is None
        proc = await session.get(DocumentProcessing, exec_id)
        assert proc is None


@pytest.mark.asyncio
async def test_concurrent_create_execution_vs_real_delete_document_deletion_wins_db(
    sample_document: Document,
) -> None:
    """Verify Scenario B with actual delete_document:

    deletion locks first and sets intent, create_execution blocks and raises error.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    service = ProcessingLifecycleService()
    session_factory = get_session_factory()
    event_deletion_locked = asyncio.Event()
    order_log: list[str] = []

    async def run_deletion() -> None:
        async with session_factory() as session:
            stmt = select(Document).where(Document.id == sample_document.id).with_for_update()
            res = await session.execute(stmt)
            doc = res.scalar_one()
            doc.deletion_pending_at = datetime.now(UTC)
            order_log.append("deletion_locked_and_pending_set")
            event_deletion_locked.set()

            # Hold lock to let create_execution attempt to acquire lock and block in PostgreSQL
            await asyncio.sleep(0.05)
            await session.commit()
            order_log.append("deletion_intent_committed")

    async def run_creation() -> str:
        await event_deletion_locked.wait()
        async with session_factory() as session:
            order_log.append("creation_attempting_lock")
            try:
                await service.create_execution(
                    session=session,
                    document_id=sample_document.id,
                    processor_name="ocr_engine",
                    processor_version="1.0.0",
                    input_sha256=sample_document.sha256,
                )
                await session.commit()
                order_log.append("creation_succeeded")
                return "succeeded"
            except DocumentPendingDeletionError:
                await session.rollback()
                order_log.append("creation_rejected_pending_deletion")
                return "rejected"

    _, creation_result = await asyncio.gather(run_deletion(), run_creation())

    assert creation_result == "rejected"
    assert order_log == [
        "deletion_locked_and_pending_set",
        "creation_attempting_lock",
        "deletion_intent_committed",
        "creation_rejected_pending_deletion",
    ]

    async with session_factory() as session:
        res = await session.execute(
            select(func.count(DocumentProcessing.id)).where(
                DocumentProcessing.document_id == sample_document.id
            )
        )
        assert res.scalar() == 0


@pytest.mark.asyncio
async def test_direct_sql_check_constraint_field_combinations_db(
    sample_document: Document,
) -> None:
    """Verify database-level check constraints enforce valid and invalid field combinations."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()

    insert_sql = (
        "INSERT INTO document_processings ("
        "  id, document_id, processor_name, processor_version, status, "
        "  input_sha256, result, error_code, error_message, "
        "  started_at, finished_at, created_at, updated_at"
        ") VALUES ("
        "  :id, :doc_id, 'proc', '1.0.0', :status, "
        "  :sha, :result, :error_code, :error_message, "
        "  :started_at, :finished_at, now(), now()"
        ");"
    )

    t1 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 12, 5, 0, tzinfo=UTC)
    sha = "a" * 64

    # 1. Valid pending
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "pending",
                "sha": sha,
                "result": None,
                "error_code": None,
                "error_message": None,
                "started_at": None,
                "finished_at": None,
            },
        )
        await conn.commit()

    # 2. Invalid pending: started_at is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "pending",
                    "sha": sha,
                    "result": None,
                    "error_code": None,
                    "error_message": None,
                    "started_at": t1,
                    "finished_at": None,
                },
            )

    # 3. Invalid pending: result is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "pending",
                    "sha": sha,
                    "result": '{"foo": "bar"}',
                    "error_code": None,
                    "error_message": None,
                    "started_at": None,
                    "finished_at": None,
                },
            )

    # 4. Valid running
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "running",
                "sha": sha,
                "result": None,
                "error_code": None,
                "error_message": None,
                "started_at": t1,
                "finished_at": None,
            },
        )
        await conn.commit()

    # 5. Invalid running: finished_at is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "running",
                    "sha": sha,
                    "result": None,
                    "error_code": None,
                    "error_message": None,
                    "started_at": t1,
                    "finished_at": t2,
                },
            )

    # 6. Valid succeeded
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "succeeded",
                "sha": sha,
                "result": '{"output": "ok"}',
                "error_code": None,
                "error_message": None,
                "started_at": t1,
                "finished_at": t2,
            },
        )
        await conn.commit()

    # 7. Invalid succeeded: result is NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "succeeded",
                    "sha": sha,
                    "result": None,
                    "error_code": None,
                    "error_message": None,
                    "started_at": t1,
                    "finished_at": t2,
                },
            )

    # 8. Valid failed
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "failed",
                "sha": sha,
                "result": None,
                "error_code": "ERR_CORRUPT",
                "error_message": "Document unreadable",
                "started_at": t1,
                "finished_at": t2,
            },
        )
        await conn.commit()

    # 9. Invalid failed: result is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "failed",
                    "sha": sha,
                    "result": '{"output": "ok"}',
                    "error_code": "ERR_FAIL",
                    "error_message": "failed",
                    "started_at": t1,
                    "finished_at": t2,
                },
            )

    # 10. Valid cancelled from pending (started_at IS NULL)
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "cancelled",
                "sha": sha,
                "result": None,
                "error_code": None,
                "error_message": "User abort",
                "started_at": None,
                "finished_at": t2,
            },
        )
        await conn.commit()

    # 11. Valid cancelled from running (started_at IS NOT NULL)
    async with engine.connect() as conn:
        await conn.execute(
            text(insert_sql),
            {
                "id": uuid.uuid4(),
                "doc_id": sample_document.id,
                "status": "cancelled",
                "sha": sha,
                "result": None,
                "error_code": None,
                "error_message": "User abort",
                "started_at": t1,
                "finished_at": t2,
            },
        )
        await conn.commit()

    # 12. Invalid cancelled: result is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "cancelled",
                    "sha": sha,
                    "result": '{"bad": true}',
                    "error_code": None,
                    "error_message": "Cancelled",
                    "started_at": t1,
                    "finished_at": t2,
                },
            )

    # 13. Invalid cancelled: error_code is NOT NULL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(insert_sql),
                {
                    "id": uuid.uuid4(),
                    "doc_id": sample_document.id,
                    "status": "cancelled",
                    "sha": sha,
                    "result": None,
                    "error_code": "NOT_ALLOWED",
                    "error_message": "Cancelled",
                    "started_at": t1,
                    "finished_at": t2,
                },
            )


@pytest.mark.asyncio
async def test_direct_sql_trigger_prevents_terminal_resurrection_db(
    sample_document: Document,
) -> None:
    """Verify PostgreSQL trigger rejects attempts to mutate or resurrect terminal rows."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    t1 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 12, 5, 0, tzinfo=UTC)
    sha = "b" * 64

    # 1. Create a succeeded record
    succ_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, result, created_at, started_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc', '1.0.0', 'succeeded', "
                "  :sha, '{\"pages\": 1}', now(), :t1, :t2, now()"
                ");"
            ),
            {"id": succ_id, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
        )
        await conn.commit()

    # Attempt to resurrect succeeded -> running via direct SQL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'running', started_at = now(), finished_at = NULL, result = NULL "
                    "WHERE id = :id;"
                ),
                {"id": succ_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state succeeded and is immutable" in str(exc_info.value)

    # 2. Create a failed record
    fail_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, error_code, error_message, "
                "  created_at, started_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc', '1.0.0', 'failed', "
                "  :sha, 'ERR_TIMEOUT', 'Timed out', now(), :t1, :t2, now()"
                ");"
            ),
            {"id": fail_id, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
        )
        await conn.commit()

    # Attempt to transition failed -> pending via direct SQL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'pending', started_at = NULL, "
                    "    finished_at = NULL, error_code = NULL "
                    "WHERE id = :id;"
                ),
                {"id": fail_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state failed and is immutable" in str(exc_info.value)

    # 3. Create a cancelled record
    cancel_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, error_message, created_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc', '1.0.0', 'cancelled', "
                "  :sha, 'Aborted', now(), :t2, now()"
                ");"
            ),
            {"id": cancel_id, "doc_id": sample_document.id, "sha": sha, "t2": t2},
        )
        await conn.commit()

    # Attempt to transition cancelled -> running via direct SQL
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'running', started_at = now(), finished_at = NULL "
                    "WHERE id = :id;"
                ),
                {"id": cancel_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state cancelled and is immutable" in str(exc_info.value)


@pytest.mark.asyncio
async def test_direct_sql_terminal_records_immutable_db(
    sample_document: Document,
) -> None:
    """Verify that records in terminal states (succeeded, failed, cancelled) are strictly immutable.

    Proves:
    1. A succeeded record cannot have its result modified while status remains succeeded.
    2. A succeeded record cannot have its error fields modified.
    3. A failed record cannot have its error message or timestamps modified.
    4. A cancelled record cannot have its fields modified.
    5. Terminal records cannot be changed through a status-preserving UPDATE.
    6. Valid nonterminal transitions continue to work.
    7. Deleting a parent Document cascades to its processing records.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    t1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 10, 5, 0, tzinfo=UTC)
    sha = "d" * 64

    # 1. Test SUCCEEDED record immutability
    succ_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, result, created_at, started_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc_a', '1.0.0', 'succeeded', "
                '  :sha, \'{"pages": 2, "text": "original"}\', now(), :t1, :t2, now()'
                ");"
            ),
            {"id": succ_id, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
        )
        await conn.commit()

    # 1a. Attempt to modify result while retaining status = succeeded
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET result = '{\"modified\": true}'::jsonb "
                    "WHERE id = :id;"
                ),
                {"id": succ_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state succeeded and is immutable" in str(exc_info.value)

    # 1b. Attempt to modify error fields on succeeded
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET error_message = 'Attempted overwrite' "
                    "WHERE id = :id;"
                ),
                {"id": succ_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state succeeded and is immutable" in str(exc_info.value)

    # 1c. Attempt status-preserving update of timestamps / metadata
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text("UPDATE document_processings SET updated_at = now() WHERE id = :id;"),
                {"id": succ_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state succeeded and is immutable" in str(exc_info.value)

    # 2. Test FAILED record immutability
    fail_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, error_code, error_message, "
                "  created_at, started_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc_a', '1.0.0', 'failed', "
                "  :sha, 'ERR_CRASH', 'Original crash message', now(), :t1, :t2, now()"
                ");"
            ),
            {"id": fail_id, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
        )
        await conn.commit()

    # 2a. Attempt to modify error_message on failed
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET error_message = 'Modified crash message' "
                    "WHERE id = :id;"
                ),
                {"id": fail_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state failed and is immutable" in str(exc_info.value)

    # 2b. Attempt to modify timestamps on failed
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text("UPDATE document_processings SET finished_at = now() WHERE id = :id;"),
                {"id": fail_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state failed and is immutable" in str(exc_info.value)

    # 3. Test CANCELLED record immutability
    cancel_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, error_message, created_at, finished_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc_a', '1.0.0', 'cancelled', "
                "  :sha, 'Original cancellation reason', now(), :t2, now()"
                ");"
            ),
            {"id": cancel_id, "doc_id": sample_document.id, "sha": sha, "t2": t2},
        )
        await conn.commit()

    # 3a. Attempt to modify fields on cancelled
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET error_message = 'New cancellation reason' "
                    "WHERE id = :id;"
                ),
                {"id": cancel_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state cancelled and is immutable" in str(exc_info.value)

    # 3b. Attempt to modify processor_name on cancelled
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET processor_name = 'tampered_processor' "
                    "WHERE id = :id;"
                ),
                {"id": cancel_id},
            )
        assert "Cannot update document_processing" in str(exc_info.value)
        assert "terminal state cancelled and is immutable" in str(exc_info.value)

    # 4. Verify valid nonterminal transitions continue to work
    nonterm_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, created_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc_a', '1.0.0', 'pending', :sha, now(), now()"
                ");"
            ),
            {"id": nonterm_id, "doc_id": sample_document.id, "sha": sha},
        )
        await conn.commit()

        # pending -> running (valid)
        await conn.execute(
            text(
                "UPDATE document_processings "
                "SET status = 'running', started_at = :t1 "
                "WHERE id = :id;"
            ),
            {"id": nonterm_id, "t1": t1},
        )
        await conn.commit()

        # running -> succeeded (valid)
        await conn.execute(
            text(
                "UPDATE document_processings "
                "SET status = 'succeeded', finished_at = :t2, result = '{\"ok\": true}'::jsonb "
                "WHERE id = :id;"
            ),
            {"id": nonterm_id, "t2": t2},
        )
        await conn.commit()

    # 5. Verify parent Document deletion triggers ON DELETE CASCADE cleanly
    session_factory = get_session_factory()
    async with session_factory() as session:
        doc_to_delete = await session.get(Document, sample_document.id)
        assert doc_to_delete is not None
        await session.delete(doc_to_delete)
        await session.commit()

    # Confirm all processing executions for this document were cascade-deleted without trigger error
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT count(*) FROM document_processings "
                "WHERE id IN (:s_id, :f_id, :c_id, :n_id);"
            ),
            {"s_id": succ_id, "f_id": fail_id, "c_id": cancel_id, "n_id": nonterm_id},
        )
        assert res.scalar() == 0


@pytest.mark.asyncio
async def test_direct_sql_trigger_valid_and_invalid_transitions_db(
    sample_document: Document,
) -> None:
    """Verify trigger enforces allowed transitions and blocks invalid non-terminal transitions."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    t1 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 12, 5, 0, tzinfo=UTC)
    sha = "c" * 64

    # 1. Create a pending record
    row_id = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO document_processings ("
                "  id, document_id, processor_name, processor_version, status, "
                "  input_sha256, created_at, updated_at"
                ") VALUES ("
                "  :id, :doc_id, 'proc', '1.0.0', 'pending', :sha, now(), now()"
                ");"
            ),
            {"id": row_id, "doc_id": sample_document.id, "sha": sha},
        )
        await conn.commit()

    # Invalid: pending -> succeeded (skipping running)
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'succeeded', started_at = :t1, "
                    "    finished_at = :t2, result = '{\"ok\": 1}' "
                    "WHERE id = :id;"
                ),
                {"id": row_id, "t1": t1, "t2": t2},
            )
        assert "Illegal status transition" in str(exc_info.value)

    # Valid: pending -> running
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "UPDATE document_processings "
                "SET status = 'running', started_at = :t1 "
                "WHERE id = :id;"
            ),
            {"id": row_id, "t1": t1},
        )
        await conn.commit()

    # Invalid: running -> pending
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError) as exc_info:
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'pending', started_at = NULL "
                    "WHERE id = :id;"
                ),
                {"id": row_id},
            )
        assert "Illegal status transition" in str(exc_info.value)

    # Valid: running -> succeeded
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "UPDATE document_processings "
                "SET status = 'succeeded', finished_at = :t2, result = '{\"ok\": true}' "
                "WHERE id = :id;"
            ),
            {"id": row_id, "t2": t2},
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_migration_0013_upgrade_downgrade_reupgrade_cycle(
    sample_document: Document,
) -> None:
    """Verify migration 0013 safely upgrades pre-existing 0012 data, downgrades, and re-upgrades.

    Proves:
    1. Database at 0012 can hold representative valid and legacy records.
    2. Upgrade to 0013 safely migrates legacy failed/cancelled records without data loss.
    3. No records are deleted; meaningful results and error codes are preserved in error_message.
    4. Downgrade to 0012 drops trigger and restores 0012 constraints without data loss.
    5. Re-upgrade to 0013 succeeds and restores trigger.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    cfg = get_alembic_config()
    await close_engine()

    # 1. Downgrade to 0012
    await asyncio.to_thread(command.downgrade, cfg, "0012_doc_processing_lifecycle")

    engine = get_engine()
    t1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 10, 5, 0, tzinfo=UTC)
    sha = "e" * 64

    id_pending = uuid.uuid4()
    id_running = uuid.uuid4()
    id_succeeded = uuid.uuid4()
    id_legacy_failed = uuid.uuid4()
    id_legacy_cancelled_res = uuid.uuid4()
    id_legacy_cancelled_err = uuid.uuid4()

    try:
        # In 0012 schema, insert valid records AND legacy records that 0012 allowed but 0013 rejects
        async with engine.connect() as conn:
            # Valid pending
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, created_at, updated_at"
                    ") VALUES (:id, :doc_id, 'proc', '1.0.0', 'pending', :sha, now(), now());"
                ),
                {"id": id_pending, "doc_id": sample_document.id, "sha": sha},
            )
            # Valid running
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, started_at, created_at, updated_at"
                    ") VALUES (:id, :doc_id, 'proc', '1.0.0', 'running', :sha, :t1, now(), now());"
                ),
                {"id": id_running, "doc_id": sample_document.id, "sha": sha, "t1": t1},
            )
            # Valid succeeded
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, result, started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'succeeded', :sha, "
                    "  '{\"pages\": 5}'::jsonb, :t1, :t2, now(), now()"
                    ");"
                ),
                {"id": id_succeeded, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
            )
            # Legacy failed: has result AND error_code (allowed in 0012, rejected in 0013)
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, result, error_code, error_message, "
                    "  started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'failed', :sha, "
                    "  '{\"partial\": \"data\"}'::jsonb, 'ERR_CRASH', "
                    "  'Engine crashed', :t1, :t2, now(), now()"
                    ");"
                ),
                {
                    "id": id_legacy_failed,
                    "doc_id": sample_document.id,
                    "sha": sha,
                    "t1": t1,
                    "t2": t2,
                },
            )
            # Legacy cancelled 1: has result (allowed in 0012, rejected in 0013)
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, result, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'cancelled', :sha, "
                    "  '{\"step\": 2}'::jsonb, :t2, now(), now()"
                    ");"
                ),
                {"id": id_legacy_cancelled_res, "doc_id": sample_document.id, "sha": sha, "t2": t2},
            )
            # Legacy cancelled 2: has error_code (allowed in 0012, rejected in 0013)
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, error_code, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'cancelled', :sha, "
                    "  'ERR_TIMEOUT', :t2, now(), now()"
                    ");"
                ),
                {"id": id_legacy_cancelled_err, "doc_id": sample_document.id, "sha": sha, "t2": t2},
            )
            await conn.commit()

        await close_engine()

        # 2. Upgrade from 0012 to final head (0013)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # 3. Verify data migration preservation
        engine = get_engine()
        async with engine.connect() as conn:
            # Check valid succeeded
            res_s = await conn.execute(
                text("SELECT result, error_code FROM document_processings WHERE id = :id;"),
                {"id": id_succeeded},
            )
            row_s = res_s.one()
            assert row_s[0] == {"pages": 5}
            assert row_s[1] is None

            # Check legacy failed: result nulled, preserved in error_message, error_code retained
            res_f = await conn.execute(
                text(
                    "SELECT result, error_code, error_message "
                    "FROM document_processings WHERE id = :id;"
                ),
                {"id": id_legacy_failed},
            )
            row_f = res_f.one()
            assert row_f[0] is None
            assert row_f[1] == "ERR_CRASH"
            assert '[migrated_result]: {"partial": "data"}' in row_f[2]

            # Check legacy cancelled 1: result nulled, preserved in error_message
            res_c1 = await conn.execute(
                text("SELECT result, error_message FROM document_processings WHERE id = :id;"),
                {"id": id_legacy_cancelled_res},
            )
            row_c1 = res_c1.one()
            assert row_c1[0] is None
            assert '[migrated_result]: {"step": 2}' in row_c1[1]

            # Check legacy cancelled 2: error_code nulled, preserved in error_message
            res_c2 = await conn.execute(
                text("SELECT error_code, error_message FROM document_processings WHERE id = :id;"),
                {"id": id_legacy_cancelled_err},
            )
            row_c2 = res_c2.one()
            assert row_c2[0] is None
            assert "[migrated_error_code]: ERR_TIMEOUT" in row_c2[1]

            # Verify structured JSON reconstruction from preserved error_message
            raw_json_f = row_f[2].split("[migrated_result]: ")[1]
            reconstructed_f = json.loads(raw_json_f)
            assert reconstructed_f == {"partial": "data"}

            raw_json_c1 = row_c1[1].split("[migrated_result]: ")[1]
            reconstructed_c1 = json.loads(raw_json_c1)
            assert reconstructed_c1 == {"step": 2}

            # Trigger exists
            res_trg = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_document_processings_transitions';"
                )
            )
            assert res_trg.scalar() == 1

        await close_engine()

        # 4. Downgrade to 0012
        await asyncio.to_thread(command.downgrade, cfg, "0012_doc_processing_lifecycle")
        engine = get_engine()
        async with engine.connect() as conn:
            res_trg = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_document_processings_transitions';"
                )
            )
            assert res_trg.scalar() == 0
        await close_engine()

        # 5. Re-upgrade to head
        await asyncio.to_thread(command.upgrade, cfg, "head")
        engine = get_engine()
        async with engine.connect() as conn:
            res_trg = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_document_processings_transitions';"
                )
            )
            assert res_trg.scalar() == 1

    finally:
        # Cleanup: ensure database is at head
        await close_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.mark.asyncio
async def test_migration_0014_terminal_immutability_upgrade_downgrade_reupgrade_cycle(
    sample_document: Document,
) -> None:
    """Verify Path A: Existing database at 0013 upgrades to 0014, downgrades, and re-upgrades.

    Proves:
    1. Starting at 0013, status-preserving updates on terminal rows are allowed by 0013 trigger.
    2. Upgrade to 0014 replaces trigger function with strict terminal immutability.
    3. In 0014, UPDATE against succeeded, failed, or cancelled records is rejected.
    4. In 0014, valid nonterminal transitions continue to work.
    5. In 0014, parent Document ON DELETE CASCADE works cleanly.
    6. Downgrade to 0013 restores 0013 trigger behavior.
    7. Re-upgrade to 0014 restores strict terminal immutability.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    cfg = get_alembic_config()
    await close_engine()

    # 1. Downgrade to 0013
    await asyncio.to_thread(command.downgrade, cfg, "0013_doc_proc_transitions")

    engine = get_engine()
    t1 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 9, 10, 5, 0, tzinfo=UTC)
    sha = "f" * 64

    id_succeeded = uuid.uuid4()
    id_failed = uuid.uuid4()
    id_cancelled = uuid.uuid4()
    id_pending = uuid.uuid4()

    try:
        # In 0013, insert terminal records
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, result, started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'succeeded', :sha, "
                    "  '{\"pages\": 1}'::jsonb, :t1, :t2, now(), now()"
                    ");"
                ),
                {"id": id_succeeded, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, error_code, error_message, "
                    "  started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'failed', :sha, "
                    "  'ERR_TIMEOUT', 'Timed out', :t1, :t2, now(), now()"
                    ");"
                ),
                {"id": id_failed, "doc_id": sample_document.id, "sha": sha, "t1": t1, "t2": t2},
            )
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, error_message, finished_at, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :doc_id, 'proc', '1.0.0', 'cancelled', :sha, "
                    "  'User cancelled', :t2, now(), now()"
                    ");"
                ),
                {"id": id_cancelled, "doc_id": sample_document.id, "sha": sha, "t2": t2},
            )
            await conn.commit()

            # In 0013: Status-preserving update on succeeded WAS permitted
            await conn.execute(
                text("UPDATE document_processings SET updated_at = now() WHERE id = :id;"),
                {"id": id_succeeded},
            )
            await conn.commit()

        await close_engine()

        # 2. Upgrade to head (0014)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        engine = get_engine()
        # 2a. In 0014: UPDATE on succeeded is now strictly rejected
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError) as exc_info:
                await conn.execute(
                    text("UPDATE document_processings SET updated_at = now() WHERE id = :id;"),
                    {"id": id_succeeded},
                )
            assert "Cannot update document_processing" in str(exc_info.value)
            assert "terminal state succeeded and is immutable" in str(exc_info.value)

        # 2b. In 0014: UPDATE on failed is rejected
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError) as exc_info:
                await conn.execute(
                    text(
                        "UPDATE document_processings SET error_message = 'Tamper' WHERE id = :id;"
                    ),
                    {"id": id_failed},
                )
            assert "Cannot update document_processing" in str(exc_info.value)
            assert "terminal state failed and is immutable" in str(exc_info.value)

        # 2c. In 0014: UPDATE on cancelled is rejected
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError) as exc_info:
                await conn.execute(
                    text(
                        "UPDATE document_processings SET error_message = 'Tamper' WHERE id = :id;"
                    ),
                    {"id": id_cancelled},
                )
            assert "Cannot update document_processing" in str(exc_info.value)
            assert "terminal state cancelled and is immutable" in str(exc_info.value)

        # 2d. In 0014: Valid nonterminal transitions work
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO document_processings ("
                    "  id, document_id, processor_name, processor_version, status, "
                    "  input_sha256, created_at, updated_at"
                    ") VALUES (:id, :doc_id, 'proc', '1.0.0', 'pending', :sha, now(), now());"
                ),
                {"id": id_pending, "doc_id": sample_document.id, "sha": sha},
            )
            await conn.commit()

            # pending -> running (allowed)
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'running', started_at = :t1 "
                    "WHERE id = :id;"
                ),
                {"id": id_pending, "t1": t1},
            )
            await conn.commit()

            # running -> succeeded (allowed)
            await conn.execute(
                text(
                    "UPDATE document_processings "
                    "SET status = 'succeeded', finished_at = :t2, result = '{\"ok\": 1}'::jsonb "
                    "WHERE id = :id;"
                ),
                {"id": id_pending, "t2": t2},
            )
            await conn.commit()

        await close_engine()

        # 3. Downgrade to 0013
        await asyncio.to_thread(command.downgrade, cfg, "0013_doc_proc_transitions")

        engine = get_engine()
        async with engine.connect() as conn:
            # In 0013, status-preserving update is allowed again
            await conn.execute(
                text("UPDATE document_processings SET updated_at = now() WHERE id = :id;"),
                {"id": id_succeeded},
            )
            await conn.commit()

        await close_engine()

        # 4. Re-upgrade to head (0014)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        engine = get_engine()
        async with engine.connect() as conn:
            # In 0014, terminal immutability is restored
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text("UPDATE document_processings SET updated_at = now() WHERE id = :id;"),
                    {"id": id_succeeded},
                )

        # 5. Verify parent document deletion cascade works cleanly in 0014
        session_factory = get_session_factory()
        async with session_factory() as session:
            doc = await session.get(Document, sample_document.id)
            assert doc is not None
            await session.delete(doc)
            await session.commit()

        async with engine.connect() as conn:
            res = await conn.execute(
                text("SELECT count(*) FROM document_processings WHERE id IN (:s1, :f1, :c1, :p1);"),
                {"s1": id_succeeded, "f1": id_failed, "c1": id_cancelled, "p1": id_pending},
            )
            assert res.scalar() == 0

    finally:
        await close_engine()
        await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.mark.asyncio
async def test_migration_fresh_database_chain_to_head() -> None:
    """Verify Path B: Migration chain from baseline to head on an isolated database."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    settings = get_settings()
    isolated_db_name = f"test_isolated_fresh_{secrets.token_hex(4)}"

    # Parse base connection URL to connect to default database for CREATE DATABASE
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE DATABASE {isolated_db_name};"))

    isolated_url = settings.DATABASE_URL.rsplit("/", 1)[0] + f"/{isolated_db_name}"

    try:
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", isolated_url)

        # Run complete migration chain from base to head
        await asyncio.to_thread(command.upgrade, cfg, "head")

        # Verify exactly one head exists and matches 0014
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        assert len(heads) == 1
        assert heads[0] == "0014_doc_proc_terminal_immut"

        # Verify isolated database tables and trigger exist
        isolated_engine = create_async_engine(isolated_url)
        async with isolated_engine.connect() as conn:
            # Check version
            res_ver = await conn.execute(text("SELECT version_num FROM alembic_version;"))
            assert res_ver.scalar() == "0014_doc_proc_terminal_immut"

            # Check trigger exists
            res_trg = await conn.execute(
                text(
                    "SELECT count(*) FROM pg_trigger "
                    "WHERE tgname = 'trg_document_processings_transitions';"
                )
            )
            assert res_trg.scalar() == 1
        await isolated_engine.dispose()
    finally:
        # Tear down isolated database cleanly
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f"DROP DATABASE {isolated_db_name} WITH (FORCE);"))
