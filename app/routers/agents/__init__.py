"""Agent router package.

Combines gateway, registry, and goal-scoped sub-routers into a single `router`
that main.py registers. Splitting into three modules keeps each under ~300 lines
with a single concern.
"""
from fastapi import APIRouter

from .gateway_routes import router as _gateway_router
from .goal_agent_routes import router as _goal_agent_router
from .registry_routes import router as _registry_router

router = APIRouter()
router.include_router(_gateway_router)
router.include_router(_registry_router)
router.include_router(_goal_agent_router)
