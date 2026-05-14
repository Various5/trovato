"""Saved search endpoints — per-user list, create, run, delete."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required
from app.database import get_session
from app.models import SavedSearch, User

router = APIRouter()


class SaveBody(BaseModel):
    name: str
    query: str
    source_ids: list[int] = []
    tags: list[str] = []
    doc_types: list[str] = []
    rerank: bool = False


@router.get("")
def list_saved(
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = session.exec(
        select(SavedSearch)
        .where(SavedSearch.user_id == user.id)
        .order_by(SavedSearch.last_used_at.desc().nullslast(), SavedSearch.id.desc())  # type: ignore[attr-defined]
    ).all()
    return [r.model_dump(mode="json") for r in rows]


@router.post("")
def create(
    body: SaveBody,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    s = SavedSearch(
        user_id=user.id,  # type: ignore[arg-type]
        name=body.name.strip() or "(unnamed)",
        query=body.query,
        source_ids=body.source_ids,
        tags=body.tags,
        doc_types=body.doc_types,
        rerank=body.rerank,
    )
    session.add(s)
    session.flush()
    return s.model_dump(mode="json")


@router.post("/{search_id}/touch")
def touch(
    search_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Update last_used_at + increment use_count."""
    s = session.get(SavedSearch, search_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    s.last_used_at = datetime.now(UTC)
    s.use_count += 1
    session.add(s)
    return s.model_dump(mode="json")


@router.delete("/{search_id}")
def delete(
    search_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    s = session.get(SavedSearch, search_id)
    if not s or s.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    session.delete(s)
    return {"status": "deleted"}
