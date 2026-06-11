"""Chat endpoints — create, list, send, rename, delete, summary."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required
from app.chat.rag import answer_question, stream_answer, summarize_document
from app.database import get_session
from app.models import Chat, ChatContextItem, ChatMessage, Document, DocumentSource, User

router = APIRouter()


class ChatCreateBody(BaseModel):
    title: str = "New chat"
    document_ids: list[int] = []
    source_ids: list[int] = []
    tags: list[str] = []


class MessageBody(BaseModel):
    content: str
    # Higher default so "in which documents…" (plural) questions retrieve enough
    # coverage across files; the prompt is still trimmed to the model's context.
    top_k: int = 12


class RenameBody(BaseModel):
    title: str


@router.post("")
def create_chat(
    body: ChatCreateBody,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    from app.auth.acl import can_see_document, can_see_source

    chat = Chat(user_id=user.id, title=body.title)  # type: ignore[arg-type]
    session.add(chat)
    session.flush()
    # Only attach context the caller is actually allowed to see — otherwise a
    # user could scope a chat to a foreign document/source id and have the RAG
    # pipeline retrieve and quote its content back to them.
    for did in body.document_ids:
        d = session.get(Document, did)
        if d and can_see_document(user, d):
            session.add(ChatContextItem(chat_id=chat.id, kind="document", ref_id=did))
    for sid in body.source_ids:
        sc = session.get(DocumentSource, sid)
        if sc and can_see_source(user, sc):
            session.add(ChatContextItem(chat_id=chat.id, kind="source", ref_id=sid))
    for t in body.tags:
        session.add(ChatContextItem(chat_id=chat.id, kind="tag", value=t))
    return chat.model_dump(mode="json")


@router.get("")
def list_chats(
    q: str | None = None,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    stmt = select(Chat).where(Chat.user_id == user.id).order_by(Chat.updated_at.desc())  # type: ignore
    rows = session.exec(stmt).all()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.title or "").lower()]
    return [r.model_dump(mode="json") for r in rows]


@router.get("/{chat_id}")
def get_chat(
    chat_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    chat = session.get(Chat, chat_id)
    if not chat or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    msgs = session.exec(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)
    ).all()
    ctx = session.exec(select(ChatContextItem).where(ChatContextItem.chat_id == chat_id)).all()
    return {
        "chat": chat.model_dump(mode="json"),
        "messages": [m.model_dump(mode="json") for m in msgs],
        "context": [c.model_dump(mode="json") for c in ctx],
    }


@router.post("/{chat_id}/messages")
async def send_message(
    chat_id: int,
    body: MessageBody,
    user: User = Depends(login_required),
) -> dict[str, Any]:
    result = await answer_question(chat_id=chat_id, user=user, question=body.content, top_k=body.top_k)
    return {
        "answer": result.answer,
        "citations": [c.__dict__ for c in result.citations],
    }


@router.post("/{chat_id}/stream")
async def stream_message(
    chat_id: int,
    body: MessageBody,
    user: User = Depends(login_required),
) -> StreamingResponse:
    async def _gen():
        async for ev in stream_answer(chat_id=chat_id, user=user, question=body.content, top_k=body.top_k):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.put("/{chat_id}")
def rename_chat(
    chat_id: int,
    body: RenameBody,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    chat = session.get(Chat, chat_id)
    if not chat or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    chat.title = body.title.strip() or "Untitled"
    session.add(chat)
    return chat.model_dump(mode="json")


@router.delete("/{chat_id}")
def delete_chat(
    chat_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    chat = session.get(Chat, chat_id)
    if not chat or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    for m in session.exec(select(ChatMessage).where(ChatMessage.chat_id == chat_id)).all():
        session.delete(m)
    for c in session.exec(select(ChatContextItem).where(ChatContextItem.chat_id == chat_id)).all():
        session.delete(c)
    session.delete(chat)
    return {"status": "deleted"}


@router.post("/summarize/{document_id}")
async def summarize(
    document_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    from app.auth.acl import can_see_document

    doc = session.get(Document, document_id)
    if not doc or not can_see_document(user, doc):
        raise HTTPException(status_code=404, detail="not found")
    text = await summarize_document(document_id)
    return {"summary": text}
