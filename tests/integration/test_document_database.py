"""Integration tests for Document entity, database constraints, ownership, and relationships."""

import secrets
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.db.models.document import Document
from app.db.models.user import User
from app.db.session import get_db_session, get_engine
from tests.integration.test_database import is_postgres_available


@pytest.fixture
async def sample_user() -> User:
    """Create and persist an isolated test user."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        user = User(
            email=f"doc_user_{secrets.token_hex(4)}@example.com",
            username=f"doc_user_{secrets.token_hex(4)}",
            password_hash="mock_argon2id_hash_for_document_test",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_document_table_columns_and_types() -> None:
    """Verify documents table exists in PostgreSQL with exact expected columns and types."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'documents' "
                "ORDER BY ordinal_position;"
            )
        )
        columns = {row[0]: (row[1], row[2]) for row in res.fetchall()}

    assert "id" in columns
    assert columns["id"] == ("uuid", "NO")

    assert "user_id" in columns
    assert columns["user_id"] == ("uuid", "NO")

    assert "storage_key" in columns
    assert columns["storage_key"] == ("character varying", "NO")

    assert "original_filename" in columns
    assert columns["original_filename"] == ("character varying", "NO")

    assert "content_type" in columns
    assert columns["content_type"] == ("character varying", "NO")

    assert "size_bytes" in columns
    assert columns["size_bytes"] == ("bigint", "NO")

    assert "sha256" in columns
    assert columns["sha256"] == ("character varying", "NO")

    assert "created_at" in columns
    assert columns["created_at"] == ("timestamp with time zone", "NO")

    assert "updated_at" in columns
    assert columns["updated_at"] == ("timestamp with time zone", "NO")


@pytest.mark.asyncio
async def test_document_indexes_in_postgresql() -> None:
    """Verify PostgreSQL index structure on documents table."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    engine = get_engine()
    async with engine.connect() as conn:
        res = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'documents';")
        )
        indexes = {row[0]: row[1] for row in res.fetchall()}

    # Primary key and unique constraint indexes
    assert "pk_documents" in indexes
    assert "uq_documents_storage_key" in indexes

    # Composite index on (user_id, created_at)
    assert "ix_documents_user_id_created_at" in indexes
    user_created_def = indexes["ix_documents_user_id_created_at"]
    assert "user_id" in user_created_def
    assert "created_at" in user_created_def

    # Redundant standalone index on user_id must NOT exist
    assert "ix_documents_user_id" not in indexes


@pytest.mark.asyncio
async def test_document_creation_and_retrieval(sample_user: User) -> None:
    """Verify inserting and querying a valid Document against live PostgreSQL."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    storage_key = secrets.token_hex(16)
    sha256_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    try:
        doc = Document(
            user_id=sample_user.id,
            storage_key=storage_key,
            original_filename="contract_2026.pdf",
            content_type="application/pdf",
            size_bytes=1048576,
            sha256=sha256_hash,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)

        assert isinstance(doc.id, uuid.UUID)
        assert doc.user_id == sample_user.id
        assert doc.storage_key == storage_key
        assert doc.original_filename == "contract_2026.pdf"
        assert doc.content_type == "application/pdf"
        assert doc.size_bytes == 1048576
        assert doc.sha256 == sha256_hash
        assert doc.created_at is not None
        assert doc.updated_at is not None

        # Query back
        res = await session.execute(select(Document).where(Document.id == doc.id))
        fetched = res.scalar_one()
        assert fetched.storage_key == storage_key

        # Cleanup
        await session.delete(doc)
        await session.commit()
    finally:
        # Cleanup user
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_multiple_documents_per_user_and_ordering(sample_user: User) -> None:
    """Verify a user can own multiple documents, retrievable in creation order."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    docs = []

    try:
        for i in range(3):
            doc = Document(
                user_id=sample_user.id,
                storage_key=secrets.token_hex(16),
                original_filename=f"doc_{i}.pdf",
                content_type="application/pdf",
                size_bytes=1000 + i,
                sha256="a" * 64,
            )
            session.add(doc)
            docs.append(doc)
        await session.commit()

        # Retrieve documents ordered by creation time descending
        stmt = (
            select(Document)
            .where(Document.user_id == sample_user.id)
            .order_by(Document.created_at.desc())
        )
        res = await session.execute(stmt)
        user_docs = res.scalars().all()
        assert len(user_docs) == 3

        # Relationship loading via selectinload
        user_stmt = (
            select(User).where(User.id == sample_user.id).options(selectinload(User.documents))
        )
        user_res = await session.execute(user_stmt)
        user_with_docs = user_res.scalar_one()
        assert len(user_with_docs.documents) == 3

        # Cleanup documents
        for d in docs:
            await session.delete(d)
        await session.commit()
    finally:
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_unique_storage_key_constraint(sample_user: User) -> None:
    """Verify PostgreSQL rejects duplicate storage_key values with IntegrityError."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    storage_key = secrets.token_hex(16)

    try:
        doc1 = Document(
            user_id=sample_user.id,
            storage_key=storage_key,
            original_filename="doc1.pdf",
            content_type="application/pdf",
            size_bytes=500,
            sha256="1" * 64,
        )
        session.add(doc1)
        await session.commit()

        doc2 = Document(
            user_id=sample_user.id,
            storage_key=storage_key,  # Duplicate!
            original_filename="doc2.pdf",
            content_type="application/pdf",
            size_bytes=600,
            sha256="2" * 64,
        )
        session.add(doc2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        assert "uq_documents_storage_key" in str(exc_info.value).lower()
        await session.rollback()

        # Cleanup doc1
        await session.delete(doc1)
        await session.commit()
    finally:
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_size",
    [-1, -100, -999999],
)
async def test_check_constraint_size_bytes_non_negative(
    sample_user: User, invalid_size: int
) -> None:
    """Verify PostgreSQL check constraint rejects negative size_bytes."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        doc = Document(
            user_id=sample_user.id,
            storage_key=secrets.token_hex(16),
            original_filename="invalid_size.pdf",
            content_type="application/pdf",
            size_bytes=invalid_size,
            sha256="3" * 64,
        )
        session.add(doc)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        assert "ck_documents_size_bytes_non_negative" in str(exc_info.value).lower()
        await session.rollback()
    finally:
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_key",
    [
        "A" * 32,  # Uppercase hex
        "g" * 32,  # Non-hex character
        "a" * 31,  # Too short (31 chars)
        "123456789012345678901234567890!@",  # Special characters
        "ab/cd/ef1234567890123456789012",  # Partitioned / path separator
        "../123456789012345678901234567",  # Traversal sequence
        " " + "a" * 31,  # Leading whitespace
        "a" * 31 + " ",  # Trailing whitespace
    ],
)
async def test_check_constraint_storage_key_hex32(sample_user: User, invalid_key: str) -> None:
    """Verify PostgreSQL check constraint rejects invalid storage_key formats."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        doc = Document(
            user_id=sample_user.id,
            storage_key=invalid_key,
            original_filename="test.pdf",
            content_type="application/pdf",
            size_bytes=100,
            sha256="4" * 64,
        )
        session.add(doc)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        assert "ck_documents_storage_key_hex32" in str(exc_info.value).lower()
    finally:
        await session.rollback()
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_sha",
    [
        "A" * 64,  # Uppercase hex
        "z" * 64,  # Non-hex character
        "f" * 63,  # Too short (63 chars)
        " " * 64,  # Whitespace
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85!",  # Special char
    ],
)
async def test_check_constraint_sha256_hex64(sample_user: User, invalid_sha: str) -> None:
    """Verify PostgreSQL check constraint rejects invalid sha256 checksums."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        doc = Document(
            user_id=sample_user.id,
            storage_key=secrets.token_hex(16),
            original_filename="test.pdf",
            content_type="application/pdf",
            size_bytes=100,
            sha256=invalid_sha,
        )
        session.add(doc)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        assert "ck_documents_sha256_hex64" in str(exc_info.value).lower()
    finally:
        await session.rollback()
        user_to_del = await session.get(User, sample_user.id)
        if user_to_del:
            await session.delete(user_to_del)
            await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_foreign_key_rejects_nonexistent_user() -> None:
    """Verify PostgreSQL rejects Document referencing a nonexistent user_id."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    nonexistent_user_id = uuid.uuid4()

    try:
        doc = Document(
            user_id=nonexistent_user_id,
            storage_key=secrets.token_hex(16),
            original_filename="orphan.pdf",
            content_type="application/pdf",
            size_bytes=100,
            sha256="5" * 64,
        )
        session.add(doc)
        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        assert "fk_documents_user_id_users" in str(exc_info.value).lower()
        await session.rollback()
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_user_deletion_restricted_when_documents_exist(sample_user: User) -> None:
    """Verify ON DELETE RESTRICT prevents deleting a User who owns documents.

    This ensures physical files on StorageBackend are never orphaned by silent
    database cascades when a User is deleted.
    """
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)

    try:
        user_id = sample_user.id
        doc_id = uuid.uuid4()
        # Create a document owned by sample_user
        doc = Document(
            id=doc_id,
            user_id=user_id,
            storage_key=secrets.token_hex(16),
            original_filename="important_document.pdf",
            content_type="application/pdf",
            size_bytes=2048,
            sha256="6" * 64,
        )
        session.add(doc)
        await session.commit()

        # Attempt to delete the User while owned documents exist
        user_to_delete = await session.get(User, user_id)
        assert user_to_delete is not None
        await session.delete(user_to_delete)

        with pytest.raises(IntegrityError) as exc_info:
            await session.commit()

        # PostgreSQL must reject deletion due to RESTRICT foreign key
        err_msg = str(exc_info.value).lower()
        is_fk_err = (
            "fk_documents_user_id_users" in err_msg or "violates foreign key constraint" in err_msg
        )
        assert is_fk_err
        await session.rollback()

        # Now explicitly delete the document first
        doc_to_delete = await session.get(Document, doc_id)
        assert doc_to_delete is not None
        await session.delete(doc_to_delete)
        await session.commit()

        # User deletion must now succeed cleanly
        user_to_delete = await session.get(User, user_id)
        assert user_to_delete is not None
        await session.delete(user_to_delete)
        await session.commit()

        # Verify user is gone
        assert await session.get(User, user_id) is None
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_user_document_isolation() -> None:
    """Verify modifying or deleting one user's document cannot affect another user's document."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)

    user1 = User(
        email=f"iso1_{secrets.token_hex(4)}@example.com",
        username=f"iso1_{secrets.token_hex(4)}",
        password_hash="mock_hash_1",
    )
    user2 = User(
        email=f"iso2_{secrets.token_hex(4)}@example.com",
        username=f"iso2_{secrets.token_hex(4)}",
        password_hash="mock_hash_2",
    )
    session.add_all([user1, user2])
    await session.commit()

    try:
        doc1 = Document(
            user_id=user1.id,
            storage_key=secrets.token_hex(16),
            original_filename="user1_file.pdf",
            content_type="application/pdf",
            size_bytes=100,
            sha256="7" * 64,
        )
        doc2 = Document(
            user_id=user2.id,
            storage_key=secrets.token_hex(16),
            original_filename="user2_file.pdf",
            content_type="application/pdf",
            size_bytes=200,
            sha256="8" * 64,
        )
        session.add_all([doc1, doc2])
        await session.commit()

        # Delete user1's document
        await session.delete(doc1)
        await session.commit()

        # Verify user2's document is completely untouched
        res = await session.execute(select(Document).where(Document.id == doc2.id))
        still_existing = res.scalar_one_or_none()
        assert still_existing is not None
        assert still_existing.user_id == user2.id
        assert still_existing.original_filename == "user2_file.pdf"

        # Cleanup user2's document
        await session.delete(still_existing)
        await session.commit()
    finally:
        u1 = await session.get(User, user1.id)
        if u1:
            await session.delete(u1)
        u2 = await session.get(User, user2.id)
        if u2:
            await session.delete(u2)
        await session.commit()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass
