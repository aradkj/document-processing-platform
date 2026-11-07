"""Unit tests for the Document domain model and metadata constraints."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Table,
    UniqueConstraint,
    inspect,
)

from app.db.models.document import Document
from app.db.models.user import User


def test_document_model_instantiation_and_defaults() -> None:
    """Verify default values and UUID generation upon Document instantiation."""
    user_id = uuid.uuid4()
    storage_key = "a" * 32
    sha256_hash = "b" * 64

    doc = Document(
        user_id=user_id,
        storage_key=storage_key,
        original_filename="sample_invoice.pdf",
        content_type="application/pdf",
        size_bytes=1048576,
        sha256=sha256_hash,
    )

    assert isinstance(doc.id, uuid.UUID)
    assert doc.user_id == user_id
    assert doc.storage_key == storage_key
    assert doc.original_filename == "sample_invoice.pdf"
    assert doc.content_type == "application/pdf"
    assert doc.size_bytes == 1048576
    assert doc.sha256 == sha256_hash


def test_document_model_explicit_attributes() -> None:
    """Verify explicitly provided UUID and timestamps are respected."""
    custom_id = uuid.uuid4()
    user_id = uuid.uuid4()
    created_at = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    updated_at = datetime(2026, 11, 6, 12, 30, 0, tzinfo=UTC)

    doc = Document(
        id=custom_id,
        user_id=user_id,
        storage_key="c" * 32,
        original_filename="scan.png",
        content_type="image/png",
        size_bytes=2048,
        sha256="d" * 64,
        created_at=created_at,
        updated_at=updated_at,
    )

    assert doc.id == custom_id
    assert doc.user_id == user_id
    assert doc.created_at == created_at
    assert doc.updated_at == updated_at


def test_document_model_repr() -> None:
    """Verify safe string representation includes id, user_id, storage_key, and filename."""
    doc_id = uuid.uuid4()
    user_id = uuid.uuid4()
    doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key="1234567890abcdef1234567890abcdef",
        original_filename="confidential.pdf",
        content_type="application/pdf",
        size_bytes=4096,
        sha256="e" * 64,
    )

    repr_str = repr(doc)
    assert str(doc_id) in repr_str
    assert str(user_id) in repr_str
    assert "1234567890abcdef1234567890abcdef" in repr_str
    assert "confidential.pdf" in repr_str


def test_document_model_table_metadata() -> None:
    """Verify table schema metadata, column types, constraints, and indexes."""
    table = Document.__table__
    assert isinstance(table, Table)
    assert table.name == "documents"

    # Column existence and types
    assert "id" in table.c
    assert "user_id" in table.c
    assert "storage_key" in table.c
    assert "original_filename" in table.c
    assert "content_type" in table.c
    assert "size_bytes" in table.c
    assert "sha256" in table.c
    assert "created_at" in table.c
    assert "updated_at" in table.c

    assert isinstance(table.c.size_bytes.type, BigInteger)
    assert table.c.user_id.nullable is False
    assert table.c.storage_key.nullable is False
    assert table.c.original_filename.nullable is False
    assert table.c.content_type.nullable is False
    assert table.c.size_bytes.nullable is False
    assert table.c.sha256.nullable is False

    # Check constraints
    ck_names = {ck.name for ck in table.constraints if isinstance(ck, CheckConstraint)}
    assert "ck_documents_size_bytes_non_negative" in ck_names
    assert "ck_documents_storage_key_hex32" in ck_names
    assert "ck_documents_sha256_hex64" in ck_names

    # Foreign key constraint with RESTRICT
    fks = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(fks) == 1
    fk = fks[0]
    assert fk.columns.keys() == ["user_id"]
    for element in fk.elements:
        assert element.target_fullname == "users.id"
        assert element.ondelete == "RESTRICT"

    # Unique constraint on storage_key
    uqs = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    uq_col_sets = [{col.name for col in uq.columns} for uq in uqs]
    assert {"storage_key"} in uq_col_sets

    # Composite index
    index_names = {idx.name for idx in table.indexes}
    assert "ix_documents_user_id_created_at" in index_names


def test_user_document_relationship_configuration() -> None:
    """Verify User and Document ORM relationships and lazy='raise' policy."""
    user_mapper = inspect(User)
    doc_mapper = inspect(Document)

    # User -> documents relationship
    assert "documents" in user_mapper.relationships
    user_docs_rel = user_mapper.relationships["documents"]
    assert user_docs_rel.lazy == "raise"
    assert user_docs_rel.back_populates == "user"
    assert not user_docs_rel.cascade.delete
    assert not user_docs_rel.cascade.delete_orphan

    # Document -> user relationship
    assert "user" in doc_mapper.relationships
    doc_user_rel = doc_mapper.relationships["user"]
    assert doc_user_rel.lazy == "raise"
    assert doc_user_rel.back_populates == "documents"
