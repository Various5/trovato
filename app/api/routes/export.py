"""Export endpoints — chats as Markdown, search hits as CSV/JSON."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlmodel import Session

from app.auth.security import login_required
from app.database import get_session
from app.models import Chat, User, UserRole
from app.services.exports import (
    chat_to_markdown,
    chat_to_pdf,
    search_hits_to_csv,
    search_hits_to_json,
)
from app.services.search_service import hybrid_search

router = APIRouter()


def _require_own_chat(session: Session, chat_id: int, user: User) -> None:
    """Reject exporting a chat the caller doesn't own (unless admin) — the chat
    read/send/delete routes check ownership, but export must too (IDOR)."""
    chat = session.get(Chat, chat_id)
    if not chat or (chat.user_id != user.id and user.role != UserRole.admin):
        raise HTTPException(status_code=404, detail="chat not found")


@router.get("/chat/{chat_id}.pdf", response_class=Response)
def export_chat_pdf(
    chat_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> Response:
    _require_own_chat(session, chat_id, user)
    try:
        return Response(
            chat_to_pdf(chat_id),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=chat-{chat_id}.pdf"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/chat/{chat_id}.md", response_class=PlainTextResponse)
def export_chat_md(
    chat_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> PlainTextResponse:
    _require_own_chat(session, chat_id, user)
    try:
        return PlainTextResponse(
            chat_to_markdown(chat_id),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=chat-{chat_id}.md"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


class SearchExportBody(BaseModel):
    query: str
    top_k: int = 50
    document_ids: list[int] | None = None
    source_ids: list[int] | None = None
    tags: list[str] | None = None


@router.post("/search.csv", response_class=Response)
async def export_search_csv(body: SearchExportBody, user: User = Depends(login_required)) -> Response:
    # user= applies the document ACL — without it the export leaked snippets +
    # paths of EVERY document regardless of the caller's visibility.
    hits = await hybrid_search(
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        tags=body.tags,
        user=user,
    )
    return Response(
        search_hits_to_csv(hits),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=search.csv"},
    )


@router.post("/search.json", response_class=Response)
async def export_search_json(body: SearchExportBody, user: User = Depends(login_required)) -> Response:
    hits = await hybrid_search(
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        tags=body.tags,
        user=user,
    )
    return Response(
        search_hits_to_json(hits),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=search.json"},
    )
