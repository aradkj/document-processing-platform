"""Pydantic schemas package."""

from app.schemas.auth import (
    LogoutResponse,
    PasswordResetConfirmRequest,
    PasswordResetConfirmResponse,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    RefreshTokenRequest,
    ResendVerificationResponse,
    TokenResponse,
    UserLoginRequest,
    UserMeResponse,
    UserProfileResponse,
    UserProfileUpdateRequest,
    UserRegisterRequest,
    UserRegisterResponse,
    VerifyEmailRequest,
    VerifyEmailResponse,
)
from app.schemas.document import DocumentResponse, PaginatedDocumentResponse

__all__ = [
    "DocumentResponse",
    "LogoutResponse",
    "PaginatedDocumentResponse",
    "PasswordResetConfirmRequest",
    "PasswordResetConfirmResponse",
    "PasswordResetRequest",
    "PasswordResetRequestResponse",
    "RefreshTokenRequest",
    "ResendVerificationResponse",
    "TokenResponse",
    "UserLoginRequest",
    "UserMeResponse",
    "UserProfileResponse",
    "UserProfileUpdateRequest",
    "UserRegisterRequest",
    "UserRegisterResponse",
    "VerifyEmailRequest",
    "VerifyEmailResponse",
]
