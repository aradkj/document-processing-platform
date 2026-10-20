"""Unit tests for structured logging and secret sanitization."""

import json
import logging

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


def test_sanitize_dict_data() -> None:
    """Verify that dictionary fields with sensitive keys are masked."""
    data = {
        "user_id": 42,
        "password": "unhashed_password",
        "api_key": "secret_key_value",
        "nested": {
            "token": "nested_token",
            "safe_field": "visible",
        },
    }
    sanitized = sanitize_data(data)
    assert sanitized["user_id"] == 42
    assert sanitized["password"] == "***"
    assert sanitized["api_key"] == "***"
    assert sanitized["nested"]["token"] == "***"
    assert sanitized["nested"]["safe_field"] == "visible"


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


def test_setup_logging() -> None:
    """Verify setup_logging configures root logger properly."""
    setup_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) >= 1
    assert isinstance(root.handlers[0].formatter, StructuredJsonFormatter)
