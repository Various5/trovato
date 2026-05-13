"""Health + diagnostics endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlmodel import select

from app.config import get_settings
from app.database import session_scope
from app.models import Chat, Document, DocumentChunk
from app.vectorstore import collection_size


router = APIRouter()


@router.get("")
def health() -> dict[str, Any]:
    s = get_settings()
    with session_scope() as session:
        doc_count = len(session.exec(select(Document)).all())
        chunk_count = len(session.exec(select(DocumentChunk)).all())
        chat_count = len(session.exec(select(Chat)).all())
    return {
        "ok": True,
        "data_dir": str(s.data_path),
        "documents": doc_count,
        "chunks": chunk_count,
        "chats": chat_count,
        "vector_count": collection_size(),
    }
