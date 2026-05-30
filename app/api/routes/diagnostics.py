"""Diagnostics endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.security import login_required
from app.models import User
from app.services.diagnostics import (
    cleanup_orphan_caches,
    find_duplicates,
    index_overview,
    lmstudio_status,
    orphan_caches,
    storage_overview,
)

router = APIRouter()


@router.get("/storage")
def storage(_user: User = Depends(login_required)) -> dict[str, Any]:
    return storage_overview()


@router.get("/index")
def index(_user: User = Depends(login_required)) -> dict[str, Any]:
    return index_overview()


@router.get("/duplicates")
def duplicates(_user: User = Depends(login_required)) -> list[dict[str, Any]]:
    return find_duplicates()


@router.get("/near-duplicates")
def near_duplicates(
    threshold: float = 0.7,
    _user: User = Depends(login_required),
) -> list[dict[str, Any]]:
    from dataclasses import asdict

    from app.services.near_dup import find_near_duplicates

    return [asdict(p) for p in find_near_duplicates(threshold=threshold)]


@router.get("/orphans")
def orphans(_user: User = Depends(login_required)) -> dict[str, Any]:
    return orphan_caches()


@router.post("/orphans/cleanup")
def orphans_cleanup(_user: User = Depends(login_required)) -> dict[str, int]:
    return cleanup_orphan_caches()


@router.get("/lmstudio")
async def lmstudio(_user: User = Depends(login_required)) -> dict[str, Any]:
    return await lmstudio_status()


@router.get("/metrics")
def metrics(_user: User = Depends(login_required)) -> dict[str, Any]:
    from app.services.metrics import queue_metrics, system_metrics

    return {"system": system_metrics(), "queue": queue_metrics()}


@router.get("/hardware")
def hardware(_user: User = Depends(login_required)) -> dict[str, Any]:
    """Detected hardware + the resolved performance tuning for this machine."""
    from app.services.hardware import tuning_summary

    return tuning_summary()


@router.get("/audit")
def audit_log(
    limit: int = 100,
    event_prefix: str | None = None,
    _user: User = Depends(login_required),
) -> list[dict[str, Any]]:
    from app.services.audit import list_events

    return list_events(limit=limit, event_prefix=event_prefix)
