"""Pydantic schemas for authentication, login, and registration."""

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import validate_password_policy

# Username validation pattern: 3-50 chars, alphanumeric, underscores, hyphens
USERNAME_REGEX = re.compile(r"^[a-zA-Z0-9_-]{3,50}$")


class UserRegisterRequest(BaseModel):
    """Payload submitted by a client to register a new user account."""

    email: EmailStr = Field(
        ...,
        description="User email address (normalized to lowercase)",
        max_length=255,
        examples=["user@example.com"],
    )
    username: str = Field(
        ...,
        description="Unique username containing letters, numbers, underscores, or hyphens",
        examples=["johndoe"],
    )
    password: str = Field(
        ...,
        description="Plaintext password to be validated against security policy and hashed",
        examples=["SecurePassw0rd!"],
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: Any) -> Any:
        """Strip whitespace and convert email to lowercase for consistent identity."""
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("username")
    @classmethod
    def validate_username_format(cls, v: str) -> str:
        """Enforce character set and format constraints on username."""
        stripped = v.strip()
        if not USERNAME_REGEX.match(stripped):
            raise ValueError(
                "Username must be 3-50 characters long and contain only letters, numbers, "
                "underscores, or hyphens."
            )
        return stripped

    @field_validator("password")
    @classmethod
    def validate_password_rules(cls, v: str) -> str:
        """Enforce platform password security policy before persistence."""
        validate_password_policy(v)
        return v


class UserRegisterResponse(BaseModel):
    """Safe public representation of a newly registered user account."""

    id: uuid.UUID = Field(..., description="Unique user identifier (UUIDv4)")
    email: str = Field(..., description="Registered email address")
    username: str = Field(..., description="Registered username")
    is_active: bool = Field(..., description="Account active status")
    is_verified: bool = Field(..., description="Account email verification status")
    created_at: datetime = Field(..., description="Account creation timestamp (UTC)")

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class UserLoginRequest(BaseModel):
    """Payload submitted by a client to authenticate and obtain an access token."""

    email: EmailStr = Field(
        ...,
        description="Registered user email address (case-insensitive)",
        max_length=255,
        examples=["user@example.com"],
    )
    password: str = Field(
        ...,
        description="Plaintext password for authentication",
        examples=["SecurePassw0rd!"],
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: Any) -> Any:
        """Strip whitespace and convert email to lowercase for consistent identity."""
        if isinstance(v, str):
            return v.strip().lower()
        return v


class TokenResponse(BaseModel):
    """Authentication response payload containing tokens and expiration metadata."""

    access_token: str = Field(..., description="Signed JWT access token")
    refresh_token: str | None = Field(
        default=None,
        description="Signed JWT refresh token",
    )
    token_type: str = Field(default="bearer", description="Token authorization type")
    expires_in: int = Field(..., description="Access token validity duration in seconds")

    model_config = ConfigDict(extra="forbid")


class RefreshTokenRequest(BaseModel):
    """Payload submitted to refresh an access token."""

    refresh_token: str = Field(
        ...,
        description="Signed JWT refresh token",
        min_length=1,
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."],
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("refresh_token")
    @classmethod
    def validate_refresh_token(cls, v: str) -> str:
        """Reject empty or whitespace-only refresh tokens."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Refresh token cannot be empty or whitespace-only.")
        return stripped


class UserProfileResponse(BaseModel):
    """Safe representation of the currently authenticated user profile."""

    id: uuid.UUID = Field(..., description="Unique user identifier (UUIDv4)")
    email: str = Field(..., description="User email address")
    username: str = Field(..., description="User username")
    is_active: bool = Field(..., description="Account active status")
    is_verified: bool = Field(..., description="Account email verification status")
    last_login_at: datetime | None = Field(
        default=None, description="Timestamp of most recent successful login (UTC)"
    )
    created_at: datetime = Field(..., description="Account creation timestamp (UTC)")
    updated_at: datetime | None = Field(
        default=None, description="Account last update timestamp (UTC)"
    )

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class UserMeResponse(UserProfileResponse):
    """User profile response model (alias for backward compatibility)."""

    pass


class UserProfileUpdateRequest(BaseModel):
    """Payload submitted to update editable profile fields of current authenticated user."""

    email: EmailStr | None = Field(
        default=None,
        description="Updated email address (normalized to lowercase)",
        max_length=255,
        examples=["updated_user@example.com"],
    )
    username: str | None = Field(
        default=None,
        description="Updated unique username (3-50 chars, alphanumeric, underscores, hyphens)",
        examples=["updated_username"],
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: Any) -> Any:
        """Strip whitespace and convert email to lowercase for consistent identity."""
        if v is None:
            raise ValueError("Email cannot be null.")
        if isinstance(v, str):
            stripped = v.strip().lower()
            if not stripped:
                raise ValueError("Email cannot be empty.")
            return stripped
        return v

    @field_validator("username")
    @classmethod
    def validate_username_format(cls, v: str | None) -> str:
        """Enforce character set and format constraints on username if provided."""
        if v is None:
            raise ValueError("Username cannot be null.")
        stripped = v.strip()
        if not USERNAME_REGEX.match(stripped):
            raise ValueError(
                "Username must be 3-50 characters long and contain only letters, numbers, "
                "underscores, or hyphens."
            )
        return stripped


class LogoutResponse(BaseModel):
    """Payload returned upon successful logout/token revocation."""

    message: str = Field(
        default="Successfully logged out.",
        description="Confirmation message of successful logout.",
    )

    model_config = ConfigDict(extra="forbid")


class VerifyEmailRequest(BaseModel):
    """Payload submitted to verify an account email address using a single-use token."""

    token: str = Field(
        ...,
        min_length=1,
        description="Opaque email verification token received via email",
        examples=["abcdef1234567890"],
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        """Reject empty or whitespace-only verification tokens."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Verification token cannot be empty or whitespace-only.")
        return stripped


class VerifyEmailResponse(BaseModel):
    """Payload returned upon successful email verification."""

    message: str = Field(
        default="Email verified successfully.",
        description="Confirmation message of successful email verification.",
    )

    model_config = ConfigDict(extra="forbid")


class ResendVerificationResponse(BaseModel):
    """Payload returned upon requesting a verification email resend."""

    message: str = Field(
        default="Verification email sent if account is unverified.",
        description="Confirmation message of verification email dispatch.",
    )

    model_config = ConfigDict(extra="forbid")
