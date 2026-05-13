"""Tag management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required
from app.database import get_session
from app.models import DocumentTagLink, Tag, User

router = APIRouter()


class TagIn(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


class MergeBody(BaseModel):
    source_ids: list[int]
    target_id: int


@router.get("")
def list_tags(
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = session.exec(select(Tag).order_by(Tag.name)).all()
    return [r.model_dump(mode="json") for r in rows]


@router.post("")
def create_tag(
    body: TagIn,
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.exec(select(Tag).where(Tag.name == body.name)).first():
        raise HTTPException(status_code=400, detail="exists")
    t = Tag(name=body.name, color=body.color, description=body.description, auto=False)
    session.add(t)
    session.flush()
    return t.model_dump(mode="json")


@router.put("/{tag_id}")
def rename_tag(
    tag_id: int,
    body: TagIn,
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    t = session.get(Tag, tag_id)
    if not t:
        raise HTTPException(status_code=404, detail="not found")
    t.name = body.name
    t.color = body.color
    t.description = body.description
    session.add(t)
    return t.model_dump(mode="json")


@router.delete("/{tag_id}")
def delete_tag(
    tag_id: int,
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    t = session.get(Tag, tag_id)
    if not t:
        raise HTTPException(status_code=404, detail="not found")
    for link in session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == tag_id)).all():
        session.delete(link)
    session.delete(t)
    return {"status": "deleted"}


@router.post("/merge")
def merge_tags(
    body: MergeBody,
    _user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    target = session.get(Tag, body.target_id)
    if not target:
        raise HTTPException(status_code=404, detail="target not found")
    moved = 0
    for sid in body.source_ids:
        if sid == body.target_id:
            continue
        for link in session.exec(select(DocumentTagLink).where(DocumentTagLink.tag_id == sid)).all():
            existing = session.exec(
                select(DocumentTagLink).where(
                    DocumentTagLink.document_id == link.document_id,
                    DocumentTagLink.tag_id == body.target_id,
                )
            ).first()
            if existing:
                session.delete(link)
            else:
                link.tag_id = body.target_id
                session.add(link)
            moved += 1
        src = session.get(Tag, sid)
        if src:
            session.delete(src)
    return {"merged": moved, "target": target.model_dump(mode="json")}
