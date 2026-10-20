"""API v1 router base.

This module provides the central APIRouter for version 1 of the platform.
Future domain modules (auth, documents, jobs, etc.) will mount their sub-routers here.
"""

from fastapi import APIRouter

api_v1_router = APIRouter(prefix="/api/v1")
