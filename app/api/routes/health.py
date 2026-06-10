"""Health + diagnostics endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import select

from app import __version__
from app.auth.security import login_required
from app.database import session_scope
from app.models import Chat, Document, DocumentChunk, User
from app.vectorstore import collection_size

router = APIRouter()


@router.get("/ping")
def ping() -> dict[str, Any]:
    """Public no-auth liveness probe — used by smoke tests + container
    orchestrators. Returns minimal info, no DB access, never auth-walled."""
    return {"ok": True, "version": __version__}


@router.get("")
def health(_user: User = Depends(login_required)) -> dict[str, Any]:
    # Auth-gated: the library counts (and previously the host data_dir) are
    # reconnaissance for an unauthenticated attacker. Use /ping for liveness.
    with session_scope() as session:
        doc_count = session.exec(select(func.count()).select_from(Document)).one()
        chunk_count = session.exec(select(func.count()).select_from(DocumentChunk)).one()
        chat_count = session.exec(select(func.count()).select_from(Chat)).one()
    return {
        "ok": True,
        "version": __version__,
        "documents": doc_count,
        "chunks": chunk_count,
        "chats": chat_count,
        "vector_count": collection_size(),
    }
