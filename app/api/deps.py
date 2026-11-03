"""Reusable FastAPI endpoint dependencies."""

import logging
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenExpiredError, TokenInvalidError, decode_access_token
from app.db.models.user import User
from app.db.session import get_db_session
from app.services.auth import get_user_by_id

logger = logging.getLogger(__name__)

# HTTPBearer security scheme (auto_error=False allows custom, safe 401 handling)
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT Bearer authorization header formatted as: Bearer <token>",
)


async def get_current_user(
    auth_credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> User:
    """Validate Bearer access token, resolve User from database, and verify active account state.

    Raises:
    - 401 Unauthorized with WWW-Authenticate: Bearer on missing, invalid, or expired tokens.
    - 401 Unauthorized if user record does not exist or user account is inactive.
    """
    if auth_credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth_credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_credentials.credentials
    try:
        payload = decode_access_token(token)
    except TokenExpiredError as exc:
        logger.info("Authentication rejected: access token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except TokenInvalidError as exc:
        logger.info("Authentication rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = uuid.UUID(subject)
    except (ValueError, TypeError) as exc:
        logger.info("Authentication rejected: invalid user_id format in sub claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await get_user_by_id(session, user_id)
    if user is None:
        logger.info("Authentication rejected: user_id=%s not found", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        logger.warning("Authentication rejected: user_id=%s is deactivated", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user
