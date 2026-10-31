"""Pydantic schemas for authentication and registration."""

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
