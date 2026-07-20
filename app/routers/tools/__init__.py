"""Tool Registry router package.

Combines tool and profile sub-routers into a single `router` that main.py registers.
Splitting into two modules keeps each under ~300 lines with a single concern.
"""
from fastapi import APIRouter

from .tool_routes import router as _tool_router
from .profile_routes import router as _profile_router

router = APIRouter()
router.include_router(_tool_router)
router.include_router(_profile_router)
