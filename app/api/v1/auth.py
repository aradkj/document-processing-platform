"""Authentication and user management API endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.models.user import User
from app.db.session import get_db_session
from app.schemas.auth import (
    LogoutResponse,
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
from app.services.auth import (
    DuplicateEmailError,
    DuplicateUsernameError,
    InactiveUserError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    authenticate_user,
    refresh_access_token,
    register_user,
    revoke_refresh_token,
    update_user_profile,
)
from app.services.email_verification import (
    InvalidVerificationTokenError,
    resend_verification_email,
    verify_email_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    description=(
        "Register a new user account with email, username, and password. "
        "The password is validated against the password policy, securely hashed "
        "using Argon2id, and the default 'user' role is assigned transactionally."
    ),
    responses={
        status.HTTP_201_CREATED: {
            "description": "User account created successfully.",
            "model": UserRegisterResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Validation error or password policy failure.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "An account with the provided email or username already exists.",
        },
    },
)
async def register(
    payload: UserRegisterRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserRegisterResponse:
    """Handle new user registration requests."""
    try:
        user = await register_user(session=session, payload=payload)
        return UserRegisterResponse.model_validate(user)
    except (DuplicateEmailError, DuplicateUsernameError) as exc:
        logger.info("Registration rejected due to duplicate identity: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="User login and access token issuance",
    description=(
        "Authenticate using registered email and password. "
        "Upon successful verification, updates the last_login_at timestamp "
        "and returns signed JWT access and refresh tokens."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Successful authentication returning JWT access and refresh tokens.",
            "model": TokenResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Authentication failed: invalid credentials or inactive account.",
        },
    },
)
async def login(
    payload: UserLoginRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    """Authenticate credentials and generate JWT access and refresh tokens."""
    try:
        _user, access_token, refresh_token, expires_in = await authenticate_user(
            session=session, payload=payload
        )
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=expires_in,
        )
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.post(
    "/refresh",
    response_model=TokenResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="Refresh access token with single-use rotation",
    description=(
        "Exchange a valid, non-expired refresh token for a newly generated "
        "access token and a rotated refresh token. The submitted refresh token is "
        "permanently consumed. Reusing an already-rotated token triggers reuse "
        "detection and invalidates the entire token family."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "New access and rotated refresh tokens issued successfully.",
            "model": TokenResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Invalid, expired, revoked, reused refresh token, or inactive user.",
        },
    },
)
async def refresh(
    payload: RefreshTokenRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TokenResponse:
    """Validate refresh token, consume it, and issue rotated tokens."""
    try:
        _user, new_access_token, new_refresh_token, expires_in = await refresh_access_token(
            session=session, refresh_token=payload.refresh_token
        )
        return TokenResponse(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            token_type="bearer",
            expires_in=expires_in,
        )
    except InvalidRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="User logout and refresh token revocation",
    description=(
        "Revoke the persistent refresh token session associated with the supplied "
        "refresh token. This operation is idempotent. After logout, the refresh token "
        "cannot be exchanged for new access tokens. Note: existing short-lived access "
        "tokens remain valid until their normal expiration time."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Session revoked successfully (or already revoked).",
            "model": LogoutResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Invalid, expired, wrong-type, or tampered refresh token.",
        },
    },
)
async def logout(
    payload: RefreshTokenRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> LogoutResponse:
    """Revoke refresh token session and log user out."""
    try:
        await revoke_refresh_token(session=session, refresh_token=payload.refresh_token)
        return LogoutResponse(message="Successfully logged out.")
    except InvalidRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


@router.get(
    "/me",
    response_model=UserMeResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current authenticated user profile",
    description=(
        "Retrieve public profile details for the currently authenticated user. "
        "Requires a valid, non-expired Bearer access token belonging to an active user."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "User profile successfully retrieved.",
            "model": UserMeResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing, invalid, expired token or inactive user.",
        },
    },
)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserMeResponse:
    """Return profile data for the active authenticated user."""
    return UserMeResponse.model_validate(current_user)


@router.patch(
    "/me",
    response_model=UserProfileResponse,
    status_code=status.HTTP_200_OK,
    summary="Update current authenticated user profile",
    description=(
        "Update editable profile fields (email, username) for the authenticated user. "
        "Email changes reset email verification status. "
        "Sensitive fields (roles, password, active status, etc.) cannot be modified."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "User profile successfully updated.",
            "model": UserProfileResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing, invalid, expired token or inactive user.",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Email or username is already taken by another account.",
        },
        422: {
            "description": "Validation error or forbidden/extra field submitted.",
        },
    },
)
async def update_me(
    payload: UserProfileUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserProfileResponse:
    """Update profile attributes for the current active user."""
    try:
        updated_user = await update_user_profile(
            session=session,
            user=current_user,
            payload=payload,
        )
        return UserProfileResponse.model_validate(updated_user)
    except (DuplicateEmailError, DuplicateUsernameError) as exc:
        logger.info("Profile update rejected due to duplicate identity: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/verify-email",
    response_model=VerifyEmailResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify user email address",
    description=(
        "Validate single-use verification token, mark token consumed, and set account "
        "is_verified state to True. Requires that token is unexpired, unconsumed, and "
        "matches the account's current email address."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Email verified successfully.",
            "model": VerifyEmailResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Token is invalid, expired, already consumed, or superseded.",
        },
        422: {
            "description": "Validation error (e.g. empty or missing token).",
        },
    },
)
async def verify_email(
    payload: VerifyEmailRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> VerifyEmailResponse:
    """Consume single-use verification token to verify account email."""
    try:
        await verify_email_token(session=session, raw_token=payload.token)
        return VerifyEmailResponse(message="Email verified successfully.")
    except InvalidVerificationTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post(
    "/resend-verification",
    response_model=ResendVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Resend verification email",
    description=(
        "Generate a new single-use verification token, invalidate any prior active tokens, "
        "and dispatch a new verification email for the authenticated user. If the account "
        "is already verified, safely acknowledges without generating a redundant email."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Verification email dispatched (or account already verified).",
            "model": ResendVerificationResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Missing, invalid, expired Bearer token or inactive account.",
        },
    },
)
async def resend_verification(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResendVerificationResponse:
    """Resend email verification token for authenticated user."""
    await resend_verification_email(session=session, user=current_user)
    return ResendVerificationResponse(message="Verification email sent if account is unverified.")
