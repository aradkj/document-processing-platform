"""Unit tests for the User domain model and metadata constraints."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Table, inspect

from app.db.models.user import User


def test_user_model_instantiation_and_defaults() -> None:
    """Verify default values and UUID generation upon model instantiation."""
    user = User(
        email="test@example.com",
        username="testuser",
        password_hash="argon2id$mock_hash_value_123",
    )

    assert isinstance(user.id, uuid.UUID)
    assert user.email == "test@example.com"
    assert user.username == "testuser"
    assert user.password_hash == "argon2id$mock_hash_value_123"
    assert user.is_active is True
    assert user.is_verified is False
    assert user.last_login_at is None


def test_user_model_explicit_attributes() -> None:
    """Verify that explicitly provided attributes are properly assigned."""
    custom_id = uuid.uuid4()
    now = datetime.now(UTC)
    user = User(
        id=custom_id,
        email="custom@example.com",
        username="customuser",
        password_hash="argon2id$custom_hash_456",
        is_active=False,
        is_verified=True,
        last_login_at=now,
    )

    assert user.id == custom_id
    assert user.email == "custom@example.com"
    assert user.username == "customuser"
    assert user.password_hash == "argon2id$custom_hash_456"
    assert user.is_active is False
    assert user.is_verified is True
    assert user.last_login_at == now


def test_user_model_repr_does_not_leak_password_hash() -> None:
    """Verify that User __repr__ includes safe identifiers and excludes password_hash."""
    user = User(
        email="safe@example.com",
        username="safeuser",
        password_hash="secret_hash_not_to_leak",
    )
    repr_str = repr(user)

    assert "safeuser" in repr_str
    assert "safe@example.com" in repr_str
    assert str(user.id) in repr_str
    assert "secret_hash_not_to_leak" not in repr_str
    assert "password_hash" not in repr_str


def test_user_table_metadata_and_constraints() -> None:
    """Verify SQLAlchemy table metadata, primary keys, and uniqueness constraints."""
    mapper = inspect(User)
    table = mapper.persist_selectable
    assert isinstance(table, Table)
    assert table.name == "users"

    pk_cols = [col.name for col in table.primary_key]
    assert pk_cols == ["id"]

    columns = table.c
    assert "id" in columns and not columns["id"].nullable
    assert "email" in columns and not columns["email"].nullable
    assert "username" in columns and not columns["username"].nullable
    assert "password_hash" in columns and not columns["password_hash"].nullable
    assert "is_active" in columns and not columns["is_active"].nullable
    assert "is_verified" in columns and not columns["is_verified"].nullable
    assert "created_at" in columns and not columns["created_at"].nullable
    assert "updated_at" in columns and not columns["updated_at"].nullable
    assert "last_login_at" in columns and columns["last_login_at"].nullable

    assert bool(columns["email"].unique) is True
    assert bool(columns["username"].unique) is True
