"""Structured, secret-safe logging configuration."""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# Sensitive pattern regexes to redact from messages and extra parameters
SENSITIVE_PATTERNS = [
    re.compile(
        r"(password|passwd|secret|token|api_?key|authorization)[\"']?\s*[:=]\s*[\"']?([^\"'\s,]+)",
        re.IGNORECASE,
    ),
    re.compile(r"://([^:]+):([^@]+)@", re.IGNORECASE),  # DB connection strings with user:password
]

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "authorization",
    "credentials",
    "db_password",
}


def sanitize_text(text: str) -> str:
    """Sanitize sensitive patterns from free-form text strings."""
    sanitized = text
    # Mask database connection passwords
    sanitized = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", sanitized)
    # Mask key-value patterns
    sanitized = re.sub(
        r"(password|passwd|secret|token|api_?key|authorization)[\"']?\s*[:=]\s*[\"']?([^\"'\s,]+)",
        r"\1=***",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def sanitize_data(data: Any) -> Any:
    """Recursively sanitize dictionaries and lists to prevent secret leakage."""
    if isinstance(data, dict):
        sanitized_dict: dict[str, Any] = {}
        for key, value in data.items():
            if str(key).lower() in SENSITIVE_KEYS:
                sanitized_dict[key] = "***"
            else:
                sanitized_dict[key] = sanitize_data(value)
        return sanitized_dict
    elif isinstance(data, list):
        return [sanitize_data(item) for item in data]
    elif isinstance(data, str):
        return sanitize_text(data)
    return data


class StructuredJsonFormatter(logging.Formatter):
    """Format logs as structured JSON with security redactions."""

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as a JSON object."""
        message = record.getMessage()
        safe_message = sanitize_text(message)

        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": safe_message,
        }

        # Include exception info if available
        if record.exc_info:
            log_data["exception"] = sanitize_text(self.formatException(record.exc_info))

        # Include custom extra fields if provided
        for key, value in record.__dict__.items():
            if key not in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "id",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                log_data[key] = sanitize_data(value)

        return json.dumps(log_data)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure root logger with structured JSON formatting and secret redaction."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(StructuredJsonFormatter())
    root_logger.addHandler(handler)

    # Keep database engine logging reasonable and prevent leaking statements
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
