"""API v1 router base.

This module provides the central APIRouter for version 1 of the platform.
Future domain modules (auth, documents, jobs, etc.) will mount their sub-routers here.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.documents import router as documents_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router, prefix="/auth")
api_v1_router.include_router(documents_router, prefix="/documents")

__all__ = ["api_v1_router"]
