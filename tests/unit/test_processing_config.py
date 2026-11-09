"""Unit tests for document processing configuration and resource limit validation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_processing_settings() -> None:
    """Verify default values for processing settings in Settings."""
    settings = Settings()
    assert settings.PROCESSING_MAX_INPUT_SIZE_BYTES == 20_971_520
    assert settings.PROCESSING_TIMEOUT_SECONDS == 60.0
    assert settings.PROCESSING_MAX_TEXT_SIZE_BYTES == 10_485_760
    assert settings.PROCESSING_MAX_PAGES == 1_000
    assert settings.PROCESSING_MAX_IMAGE_PIXELS == 50_000_000
    assert settings.PROCESSING_MAX_METADATA_SIZE_BYTES == 65_536


@pytest.mark.parametrize(
    ("field", "invalid_val"),
    [
        ("PROCESSING_MAX_INPUT_SIZE_BYTES", 0),
        ("PROCESSING_MAX_INPUT_SIZE_BYTES", 524_288_001),  # > 500 MiB
        ("PROCESSING_TIMEOUT_SECONDS", 0.5),  # < 1.0
        ("PROCESSING_TIMEOUT_SECONDS", 3601.0),  # > 3600
        ("PROCESSING_MAX_TEXT_SIZE_BYTES", 512),  # < 1024
        ("PROCESSING_MAX_TEXT_SIZE_BYTES", 104_857_601),  # > 100 MiB
        ("PROCESSING_MAX_PAGES", 0),  # < 1
        ("PROCESSING_MAX_PAGES", 50_001),  # > 50000
        ("PROCESSING_MAX_IMAGE_PIXELS", 500),  # < 1000
        ("PROCESSING_MAX_IMAGE_PIXELS", 500_000_001),  # > 500 MP
        ("PROCESSING_MAX_METADATA_SIZE_BYTES", 500),  # < 1024
        ("PROCESSING_MAX_METADATA_SIZE_BYTES", 1_048_577),  # > 1 MiB
    ],
)
def test_processing_settings_validation_boundaries(field: str, invalid_val: int | float) -> None:
    """Verify out-of-boundary values are rejected by Pydantic validation."""
    kwargs: dict[str, Any] = {field: invalid_val}
    with pytest.raises(ValidationError):
        Settings(**kwargs)
