from __future__ import annotations

from fastapi import APIRouter

from . import items, lifecycle, timeseries

# 통합 router — main.py 가 단일 `production.router` 로 include
router = APIRouter()
router.include_router(lifecycle.router)
router.include_router(items.router)
router.include_router(timeseries.router)

__all__ = ["router"]
