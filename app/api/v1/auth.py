"""Authentication and user management API endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.auth import UserRegisterRequest, UserRegisterResponse
from app.services.auth import (
    DuplicateEmailError,
    DuplicateUsernameError,
    register_user,
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
