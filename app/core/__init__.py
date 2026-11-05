"""Core module containing application configuration, logging, and security."""

from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.security import (
    PasswordPolicyError,
    TokenExpiredError,
    TokenInvalidError,
    TokenSecurityError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_password_hash,
    hash_email_verification_token,
    hash_password_reset_token,
    validate_password_policy,
    verify_dummy_password,
    verify_password,
)

__all__ = [
    "PasswordPolicyError",
    "Settings",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenSecurityError",
    "create_access_token",
    "create_refresh_token",
    "decode_access_token",
    "decode_refresh_token",
    "generate_email_verification_token",
    "generate_password_reset_token",
    "get_password_hash",
    "get_settings",
    "hash_email_verification_token",
    "hash_password_reset_token",
    "setup_logging",
    "validate_password_policy",
    "verify_dummy_password",
    "verify_password",
]
