"""RAG pipeline: retrieval → prompt building → answer with citations.

The system prompt instructs the model to:
* answer only from the provided context,
* cite every claim with [#] markers referring to the source list,
* admit when the answer isn't in the documents (no hallucinations).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from sqlmodel import select

from app.database import session_scope
from app.llm import LMStudioError, get_client
from app.models import (
    Chat,
    ChatContextItem,
    ChatMessage,
    Document,
    DocumentChunk,
    User,
    UserMemory,
    UserSetting,
)
from app.services.search_service import SearchHit, hybrid_search
from app.utils.logging import logger


SYSTEM_PROMPT = """You are LocalDoc Intelligence, a careful local assistant that answers \
questions strictly using the provided document context.

Rules:
1. Use ONLY the supplied context. If the answer is not in the context, say so plainly.
2. Cite every fact with bracketed numbers, e.g. [1], that map to the SOURCES list.
3. Prefer concise, accurate answers. Quote short snippets when helpful.
4. If the user explicitly asks for opinions or summaries beyond the documents, \
make clear that the answer is reasoning, not from sources.
5. Always answer in the user's language."""


@dataclass
class Citation:
    n: int
    document_id: int
    chunk_id: int
    filename: str
    path: str
    page_from: int
    page_to: int
    snippet: str


@dataclass
class RAGResult:
    answer: str
    citations: list[Citation]


def _build_context_block(hits: list[SearchHit], max_chars: int = 8000) -> tuple[str, list[Citation]]:
    parts: list[str] = []
    cites: list[Citation] = []
    used = 0
    for i, h in enumerate(hits, start=1):
        block = (
            f"[{i}] {h.filename} (p.{h.page_from}"
            f"{'-' + str(h.page_to) if h.page_to != h.page_from else ''})\n{h.snippet}\n"
        )
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
        cites.append(
            Citation(
                n=i,
                document_id=h.document_id,
                chunk_id=h.chunk_id,
                filename=h.filename,
                path=h.path,
                page_from=h.page_from,
                page_to=h.page_to,
                snippet=h.snippet,
            )
        )
    return "\n".join(parts), cites


def _gather_user_memory(user_id: int, session) -> str:
    memories = session.exec(
        select(UserMemory).where(UserMemory.user_id == user_id, UserMemory.confirmed == True)  # noqa: E712
    ).all()
    if not memories:
        return ""
    lines = [f"- {m.key}: {m.value}" for m in memories if not m.sensitive]
    return "\n".join(lines)


def _chat_context_filters(chat_id: int, session) -> dict[str, Any]:
    items = session.exec(
        select(ChatContextItem).where(ChatContextItem.chat_id == chat_id)
    ).all()
    out: dict[str, list] = {"document_ids": [], "source_ids": [], "tags": []}
    for it in items:
        if it.kind == "document" and it.ref_id:
            out["document_ids"].append(it.ref_id)
        elif it.kind == "source" and it.ref_id:
            out["source_ids"].append(it.ref_id)
        elif it.kind == "tag" and it.value:
            out["tags"].append(it.value)
    return {k: v for k, v in out.items() if v}


async def answer_question(
    *,
    chat_id: int,
    user: User,
    question: str,
    top_k: int = 8,
    history_window: int = 6,
) -> RAGResult:
    """Run a full RAG turn and persist the assistant message with citations."""

    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat or chat.user_id != user.id:
            raise ValueError("chat not found")
        filters = _chat_context_filters(chat_id, session)
        memory_block = _gather_user_memory(user.id, session)
        history = session.exec(
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.id.desc())
            .limit(history_window)
        ).all()
        history.reverse()
        # Save user message
        user_msg = ChatMessage(chat_id=chat_id, role="user", content=question)
        session.add(user_msg)
        # Update chat timestamps
        from datetime import datetime, timezone

        chat.updated_at = datetime.now(timezone.utc)
        session.add(chat)

    hits = await hybrid_search(
        question,
        top_k=top_k,
        document_ids=filters.get("document_ids"),
        source_ids=filters.get("source_ids"),
        tags=filters.get("tags"),
        user=user,
    )

    context_block, citations = _build_context_block(hits)

    sys = SYSTEM_PROMPT
    if memory_block:
        sys += "\n\nUser memory (use only if relevant):\n" + memory_block

    messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})

    if context_block:
        prompt_user = (
            "SOURCES:\n" + context_block + "\n\nQUESTION:\n" + question +
            "\n\nAnswer using the SOURCES above. Cite with [#]."
        )
    else:
        prompt_user = (
            "I have no relevant documents indexed for this question. "
            "Please respond honestly that no source was found.\n\nQUESTION:\n" + question
        )
    messages.append({"role": "user", "content": prompt_user})

    client = get_client()
    try:
        answer_text = await client.chat(messages, temperature=0.2, max_tokens=900)
    except LMStudioError as e:
        answer_text = f"_LM Studio is not reachable ({e}). Configure it in Settings → LM Studio._"
    except Exception as e:
        logger.exception("rag chat failed: {}", e)
        answer_text = f"_Internal error while generating answer: {e}_"

    # Persist assistant message
    with session_scope() as session:
        sources = [
            {
                "n": c.n,
                "document_id": c.document_id,
                "chunk_id": c.chunk_id,
                "filename": c.filename,
                "path": c.path,
                "page_from": c.page_from,
                "page_to": c.page_to,
                "snippet": c.snippet,
            }
            for c in citations
        ]
        msg = ChatMessage(
            chat_id=chat_id, role="assistant", content=answer_text, sources=sources
        )
        session.add(msg)

    return RAGResult(answer=answer_text, citations=citations)


async def stream_answer(
    *,
    chat_id: int,
    user: User,
    question: str,
    top_k: int = 8,
    history_window: int = 6,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-shaped events: ``{"type": "...", ...}``.

    Event types:
        sources: payload with the citation list (before any token arrives)
        token:   payload with a single text delta
        done:    final marker, includes the full text (persistence already happened)
        error:   an error string
    """
    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat or chat.user_id != user.id:
            yield {"type": "error", "error": "chat not found"}
            return
        filters = _chat_context_filters(chat_id, session)
        memory_block = _gather_user_memory(user.id, session)
        history = session.exec(
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.id.desc())
            .limit(history_window)
        ).all()
        history.reverse()
        session.add(ChatMessage(chat_id=chat_id, role="user", content=question))
        from datetime import datetime, timezone

        chat.updated_at = datetime.now(timezone.utc)
        session.add(chat)

    hits = await hybrid_search(
        question,
        top_k=top_k,
        document_ids=filters.get("document_ids"),
        source_ids=filters.get("source_ids"),
        tags=filters.get("tags"),
        user=user,
    )
    context_block, citations = _build_context_block(hits)

    sys = SYSTEM_PROMPT
    if memory_block:
        sys += "\n\nUser memory (use only if relevant):\n" + memory_block

    messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})
    if context_block:
        messages.append(
            {
                "role": "user",
                "content": "SOURCES:\n" + context_block + "\n\nQUESTION:\n" + question
                + "\n\nAnswer using the SOURCES above. Cite with [#].",
            }
        )
    else:
        messages.append(
            {
                "role": "user",
                "content": "No documents are indexed for this question. Be honest about it.\n\n"
                + question,
            }
        )

    yield {
        "type": "sources",
        "citations": [c.__dict__ for c in citations],
    }

    full: list[str] = []
    client = get_client()
    try:
        async for piece in client.chat_stream(messages, temperature=0.2, max_tokens=900):
            full.append(piece)
            yield {"type": "token", "text": piece}
    except LMStudioError as e:
        msg = f"_LM Studio not reachable: {e}_"
        full.append(msg)
        yield {"type": "token", "text": msg}
    except Exception as e:
        logger.exception("rag stream failed: {}", e)
        msg = f"_Internal error: {e}_"
        full.append(msg)
        yield {"type": "token", "text": msg}

    answer_text = "".join(full)
    with session_scope() as session:
        sources = [c.__dict__ for c in citations]
        session.add(
            ChatMessage(chat_id=chat_id, role="assistant", content=answer_text, sources=sources)
        )

    yield {"type": "done", "answer": answer_text}


async def summarize_document(document_id: int, *, max_tokens: int = 600) -> str:
    """One-shot summary of a document using its first N chunks."""
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if not doc:
            raise ValueError("document not found")
        chunks = session.exec(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.page_from)
            .limit(12)
        ).all()
        text = "\n\n".join(c.text for c in chunks)

    if not text:
        return "(no extracted text)"
    prompt = (
        "Summarize the following document into a structured brief: "
        "title, purpose, key facts, dates, parties, financial figures (if any), "
        "and 3-5 bullet takeaways. Keep it under 200 words.\n\nDOCUMENT:\n" + text[:9000]
    )
    client = get_client()
    try:
        return await client.chat(
            [{"role": "user", "content": prompt}], temperature=0.1, max_tokens=max_tokens
        )
    except Exception as e:
        return f"_Summary failed: {e}_"


def _ensure_settings(user: User, session) -> UserSetting:
    s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
    if not s:
        s = UserSetting(user_id=user.id)
        session.add(s)
        session.flush()
    return s
