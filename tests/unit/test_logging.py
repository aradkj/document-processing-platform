"""Unit tests for structured logging and secret sanitization."""

import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.core.logging import (
    StructuredJsonFormatter,
    sanitize_data,
    sanitize_text,
    setup_logging,
)


def test_sanitize_database_url() -> None:
    """Verify that passwords embedded in database URLs are redacted."""
    raw_url = "postgresql+asyncpg://admin:supersecretpassword123@db.example.com:5432/platform"
    sanitized = sanitize_text(raw_url)
    assert "supersecretpassword123" not in sanitized
    assert "admin:***@db.example.com" in sanitized


def test_sanitize_text_sensitive_keys() -> None:
    """Verify that key-value patterns with sensitive names are redacted."""
    raw_text = "Failed with password: my_password_456 and token: abc-123-secret"
    sanitized = sanitize_text(raw_text)
    assert "my_password_456" not in sanitized
    assert "abc-123-secret" not in sanitized
    assert "password=***" in sanitized
    assert "token=***" in sanitized


def test_sanitize_bearer_token() -> None:
    """Verify that Authorization Bearer tokens are redacted."""
    raw_header = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    )
    sanitized = sanitize_text(raw_header)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in sanitized
    assert "Authorization: Bearer ***" in sanitized


def test_sanitize_url_query_parameters() -> None:
    """Verify that sensitive URL query parameters are redacted."""
    raw_url = "https://api.example.com/callback?token=secret_token_123&client_id=client456&api_key=key_789"
    sanitized = sanitize_text(raw_url)
    assert "secret_token_123" not in sanitized
    assert "key_789" not in sanitized
    assert "?token=***" in sanitized
    assert "&api_key=***" in sanitized
    assert "client_id=client456" in sanitized


def test_sanitize_dict_data() -> None:
    """Verify that dictionary fields with sensitive keys are masked."""
    data = {
        "user_id": 42,
        "password": "unhashed_password",
        "api_key": "secret_key_value",
        "client_secret": "my_client_secret",
        "nested": {
            "token": "nested_token",
            "access_token": "access_xyz",
            "refresh_token": "refresh_abc",
            "safe_field": "visible",
        },
    }
    sanitized = sanitize_data(data)
    assert sanitized["user_id"] == 42
    assert sanitized["password"] == "***"
    assert sanitized["api_key"] == "***"
    assert sanitized["client_secret"] == "***"
    assert sanitized["nested"]["token"] == "***"
    assert sanitized["nested"]["access_token"] == "***"
    assert sanitized["nested"]["refresh_token"] == "***"
    assert sanitized["nested"]["safe_field"] == "visible"


def test_sanitize_collections_tuples_and_sets() -> None:
    """Verify that sets, lists, and tuples are recursively sanitized."""
    data = [
        {"password": "secret_password"},
        ("postgresql+asyncpg://user:pass123@host:5432/db",),
    ]
    sanitized = sanitize_data(data)
    assert sanitized[0]["password"] == "***"
    assert "pass123" not in sanitized[1][0]


def test_structured_json_formatter() -> None:
    """Verify that StructuredJsonFormatter outputs valid JSON with required fields."""
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Connecting to database at postgresql://user:secret@localhost:5432/db",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert "timestamp" in parsed
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test_logger"
    assert "secret" not in parsed["message"]
    assert "user:***@localhost" in parsed["message"]


def test_structured_json_formatter_handles_non_serializable_objects() -> None:
    """Verify that non-serializable objects in extra fields are safely serialized."""
    formatter = StructuredJsonFormatter()
    custom_uuid = uuid4()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=20,
        msg="Processing job",
        args=(),
        exc_info=None,
    )
    record.job_id = custom_uuid
    record.event_time = datetime.now(UTC)

    formatted = formatter.format(record)
    parsed = json.loads(formatted)
    assert parsed["job_id"] == str(custom_uuid)
    assert "event_time" in parsed


def test_setup_logging() -> None:
    """Verify setup_logging configures root logger properly."""
    setup_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) >= 1
    assert isinstance(root.handlers[0].formatter, StructuredJsonFormatter)
