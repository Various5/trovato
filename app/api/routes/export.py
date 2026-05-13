"""Export endpoints — chats as Markdown, search hits as CSV/JSON."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from app.auth.security import login_required
from app.models import User
from app.services.exports import (
    chat_to_markdown,
    chat_to_pdf,
    search_hits_to_csv,
    search_hits_to_json,
)
from app.services.search_service import hybrid_search

router = APIRouter()


@router.get("/chat/{chat_id}.pdf", response_class=Response)
def export_chat_pdf(chat_id: int, _user: User = Depends(login_required)) -> Response:
    try:
        return Response(
            chat_to_pdf(chat_id),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=chat-{chat_id}.pdf"},
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/chat/{chat_id}.md", response_class=PlainTextResponse)
def export_chat_md(chat_id: int, _user: User = Depends(login_required)) -> PlainTextResponse:
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
async def export_search_csv(body: SearchExportBody, _user: User = Depends(login_required)) -> Response:
    hits = await hybrid_search(
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        tags=body.tags,
    )
    return Response(
        search_hits_to_csv(hits),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=search.csv"},
    )


@router.post("/search.json", response_class=Response)
async def export_search_json(body: SearchExportBody, _user: User = Depends(login_required)) -> Response:
    hits = await hybrid_search(
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        tags=body.tags,
    )
    return Response(
        search_hits_to_json(hits),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=search.json"},
    )
