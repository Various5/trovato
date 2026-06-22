"""RAG pipeline: retrieval → prompt building → answer with citations.

The system prompt instructs the model to:
* answer only from the provided context,
* cite every claim with [#] markers referring to the source list,
* admit when the answer isn't in the documents (no hallucinations).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.llm import LMStudioError, get_client
from app.llm.lmstudio import context_char_budget
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

SYSTEM_PROMPT = """You are Trovato, a careful local assistant that answers \
questions strictly using the provided document context.

Rules:
1. ALWAYS write your answer in the SAME language the user used in their latest question. \
If they ask in German, answer in German; if in English, answer in English. Never switch \
to another language on your own.
2. Use ONLY the supplied context. If the answer is not in the context, say so plainly.
3. Cite every fact with bracketed numbers, e.g. [1], that map to the SOURCES list. \
The SOURCES are numbered [1] to [N] (N = the number of sources shown). ONLY cite numbers \
in that range — NEVER invent a higher number, and NEVER copy a bracketed number that \
appears inside the document text (footnotes, steps, references) as if it were a citation. \
If only one SOURCE is provided, cite [1] for every fact.
4. Some sources are marked "IMAGE" — that text is a description of an image that is \
actually embedded in the document. Treat such a source as proof that the document \
contains that image. When the user asks which documents contain a picture/photo/image \
of something, answer from these IMAGE sources and cite them. Do NOT claim there are no \
images when IMAGE sources are present.
5. Each numbered SOURCE is ONE document (its relevant pages are listed under it). \
For broad questions — "which documents…", "compare…", "list…", "how many…" — consider \
ALL provided sources, synthesise across them, and name every relevant document with its \
citation. Don't answer from just the first one or two.
6. Prefer concise, accurate answers. Quote short snippets when helpful.
7. If the user explicitly asks for opinions or summaries beyond the documents, make \
clear that the answer is reasoning, not from sources.
8. The LIBRARY OVERVIEW line states how many documents the user's whole library \
really contains. The numbered SOURCES are only the few excerpts retrieved for the \
current question — NEVER present the number of SOURCES as the size of the library or \
of "your context/index". When the user asks how many documents you have, know, or can \
access, answer with the LIBRARY OVERVIEW numbers (and you may add that excerpts from \
N documents were retrieved for this particular question)."""


# Common function words used to guess the user's language for short queries —
# detect_language() in tagging.py only looks for a few words with surrounding
# spaces and misses most chat questions, so we use a wider, space-free set here.
_DE_HINTS = {
    "der",
    "die",
    "das",
    "und",
    "ist",
    "von",
    "den",
    "dem",
    "ein",
    "eine",
    "einem",
    "einen",
    "welche",
    "welchem",
    "welchen",
    "welcher",
    "welches",
    "dokument",
    "dokumente",
    "dokumenten",
    "wird",
    "wurde",
    "hat",
    "haben",
    "habe",
    "es",
    "oder",
    "auf",
    "gibt",
    "wie",
    "wo",
    "was",
    "wer",
    "warum",
    "nicht",
    "mit",
    "für",
    "über",
    "aufgeführt",
    "aufgefuehrt",
    "bild",
    "bilder",
    "foto",
    "fotos",
    "zeigt",
    "kannst",
    "mir",
    "mein",
    "meine",
    "sind",
    "im",
    "zur",
    "zum",
    "auch",
}
_EN_HINTS = {
    "the",
    "is",
    "are",
    "which",
    "what",
    "where",
    "who",
    "document",
    "documents",
    "show",
    "have",
    "has",
    "image",
    "images",
    "photo",
    "photos",
    "picture",
    "does",
    "list",
    "find",
    "about",
    "with",
    "of",
    "in",
    "do",
    "can",
    "you",
}
_LANG_NAMES = {"de": "German (Deutsch)", "en": "English"}


def _detect_lang(text: str) -> str | None:
    """Best-effort language guess for a chat question (German vs. English)."""
    t = (text or "").lower()
    if any(ch in t for ch in "äöüß"):
        return "de"
    words = set(re.findall(r"[a-zäöüß]+", t))
    de = len(words & _DE_HINTS)
    en = len(words & _EN_HINTS)
    if de > en:
        return "de"
    if en > de:
        return "en"
    return None


def _lang_directive(question: str) -> str:
    """An explicit 'answer in <language>' line — the strongest signal we can
    give a model that otherwise defaults to English."""
    name = _LANG_NAMES.get(_detect_lang(question) or "")
    return f"\n\nWrite your entire answer in {name}." if name else ""


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


# Library-wide questions ("which documents…", "compare all…", "list every…")
# should pull from MANY documents, not drill into one. Detect them to widen
# retrieval and favour cross-document breadth.
# Trimmed to genuine aggregation / cross-document signals. The dropped tokens
# (list, most, common, count, each, jede[rsn]?, häufig) appear too often in
# ordinary single-fact questions and used to flip them into the breadth plan —
# which then capped each document to one chunk and threw away the very chunk
# that answered the question on a reworded follow-up.
_BROAD_RX = re.compile(
    r"\b("
    r"all|every|across|compare|comparison|overview|summar(?:y|ize|ise)|"
    r"which docs?|which documents?|how many|"
    r"alle|sämtliche|welche dokumente|welche unterlagen|vergleich|"
    r"überblick|zusammenfass|auflisten|wie viele|insgesamt"
    r")\b",
    re.IGNORECASE,
)


def _is_broad_query(question: str) -> bool:
    return bool(_BROAD_RX.search(question or ""))


def _retrieval_plan(question: str, base_top_k: int) -> tuple[int, int]:
    """(effective_top_k, max_chunks_per_doc). Broad questions retrieve far more
    candidates and take *fewer* chunks per document → maximum library coverage.

    The broad cap is 2 (not 1): one chunk per document is so aggressive that a
    cited document's actual answer chunk is often dropped, so two phrasings of
    the same intent return disjoint sources. Two keeps breadth while leaving
    enough depth that the answer chunk survives."""
    if _is_broad_query(question):
        return max(base_top_k, 40), 2
    return base_top_k, 3


def _chunk_label(kind: str, pages: str) -> str:
    if kind == "image_description":
        return f"({pages}, IMAGE — the text describes an image embedded here)"
    if kind == "table":
        return f"({pages}, TABLE)"
    if kind == "ocr_text":
        return f"({pages}, scanned/OCR text)"
    return f"({pages})"


def _build_context_block(
    hits: list[SearchHit], max_chars: int = 8000, *, max_per_doc: int = 3
) -> tuple[str, list[Citation]]:
    """Group retrieved chunks by document and assign ONE citation number per
    document (not per chunk), including up to ``max_per_doc`` of its chunks.

    This gives the model breadth across the library and stops the same PDF being
    cited as [1], [7], [13] with different snippets — each document is one source
    with its pages listed. Documents are visited in best-hit-score order.
    """
    by_doc: dict[int, list[SearchHit]] = {}
    order: list[int] = []
    for h in hits:
        if h.document_id not in by_doc:
            by_doc[h.document_id] = []
            order.append(h.document_id)
        by_doc[h.document_id].append(h)

    # Feed the model the actual chunk body, not the 220-char UI highlight
    # preview — otherwise the right document is cited but its answer text is
    # invisible to the model. Focused questions (max_per_doc > 1) want depth, so
    # take nearly the whole ~1100-token chunk; broad "which documents…" queries
    # (max_per_doc == 1, now 2) want many documents, so cap each chunk shorter
    # to fit more sources in the budget.
    per_chunk_cap = 1200 if max_per_doc <= 1 else 3600

    parts: list[str] = []
    cites: list[Citation] = []
    used = 0
    for did in order:
        doc_hits = by_doc[did][:max_per_doc]
        best = doc_hits[0]
        seg_lines: list[str] = []
        pages_used: list[int] = []
        for h in doc_hits:
            pages = f"p.{h.page_from}" + (f"-{h.page_to}" if h.page_to != h.page_from else "")
            kind = getattr(h, "source", "") or ""
            body = ((getattr(h, "text", "") or h.snippet) or "")[:per_chunk_cap]
            seg_lines.append(f"  {_chunk_label(kind, pages)}: {body}")
            pages_used.append(h.page_from)
            if h.page_to:
                pages_used.append(h.page_to)
        num = len(cites) + 1
        block = f"[{num}] {best.filename}\n" + "\n".join(seg_lines) + "\n"
        if used + len(block) > max_chars and cites:
            break
        parts.append(block)
        used += len(block)
        cites.append(
            Citation(
                n=num,
                document_id=did,
                chunk_id=best.chunk_id,
                filename=best.filename,
                path=best.path,
                page_from=min(pages_used),
                page_to=max(pages_used),
                snippet=best.snippet,
            )
        )
    return "\n".join(parts), cites


def _library_overview(user: User, filters: dict[str, Any], session) -> str:
    """True index stats for the system prompt — one cheap COUNT query.

    The model only ever sees the few SOURCES retrieved for the current
    question. Without the real numbers it answers meta questions like
    "wieviele Dokumente hast du?" with the SOURCES count (e.g. 10) — wildly
    wrong for a 100+ file library. ACL-filtered, so a non-admin's overview
    only counts documents they can actually see.
    """
    from sqlalchemy import func as _func

    from app.auth.acl import filter_documents
    from app.models import DocumentStatus

    try:
        row = session.exec(
            filter_documents(
                select(
                    _func.count(Document.id),
                    _func.coalesce(_func.sum(Document.page_count), 0),
                ).where(Document.status == DocumentStatus.indexed),
                user,
            )
        ).one()
        doc_count, page_count = int(row[0] or 0), int(row[1] or 0)
    except Exception as e:  # never break a chat over a stats line
        logger.debug("library overview failed: {}", e)
        return ""
    text = (
        f"LIBRARY OVERVIEW: the user's library contains {doc_count} indexed documents "
        f"({page_count} pages) in total. The SOURCES below are ONLY the excerpts "
        "retrieved for this question."
    )
    if filters:
        text += (
            " Note: this chat is restricted by context filters to a subset of the "
            "library; retrieval only searches that subset."
        )
    return text


def _gather_user_memory(user_id: int, session) -> str:
    memories = session.exec(
        select(UserMemory).where(UserMemory.user_id == user_id, UserMemory.confirmed == True)  # noqa: E712
    ).all()
    if not memories:
        return ""
    lines = [f"- {m.key}: {m.value}" for m in memories if not m.sensitive]
    return "\n".join(lines)


def _chat_context_filters(chat_id: int, session) -> dict[str, Any]:
    items = session.exec(select(ChatContextItem).where(ChatContextItem.chat_id == chat_id)).all()
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
    top_k: int = 15,
    history_window: int = 6,
) -> RAGResult:
    """Run a full RAG turn and persist the assistant message with citations."""

    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat or chat.user_id != user.id:
            raise ValueError("chat not found")
        filters = _chat_context_filters(chat_id, session)
        memory_block = _gather_user_memory(user.id, session)
        overview_block = _library_overview(user, filters, session)
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
        from datetime import datetime

        chat.updated_at = datetime.now(UTC)
        session.add(chat)

    eff_top_k, max_per_doc = _retrieval_plan(question, top_k)
    # Overlap the context-length probe with retrieval (they're independent) and
    # reuse a single client for the whole turn → faster time-to-first-token.
    client = get_client()
    hits, ctx_tokens = await asyncio.gather(
        hybrid_search(
            question,
            top_k=eff_top_k,
            document_ids=filters.get("document_ids"),
            source_ids=filters.get("source_ids"),
            tags=filters.get("tags"),
            user=user,
        ),
        client.model_context_length(),
    )

    context_block, citations = _build_context_block(
        hits,
        max_chars=context_char_budget(ctx_tokens),
        max_per_doc=max_per_doc,
    )

    sys = SYSTEM_PROMPT
    if overview_block:
        sys += "\n\n" + overview_block
    if memory_block:
        sys += "\n\nUser memory (use only if relevant):\n" + memory_block

    messages: list[dict[str, Any]] = [{"role": "system", "content": sys}]
    for m in history:
        if m.role in ("user", "assistant"):
            messages.append({"role": m.role, "content": m.content})

    if context_block:
        prompt_user = (
            "SOURCES:\n"
            + context_block
            + "\n\nQUESTION:\n"
            + question
            + "\n\nAnswer using the SOURCES above. Cite with [#]. The SOURCES are "
            "authoritative for THIS question and supersede anything stated in earlier turns."
            + _lang_directive(question)
        )
    else:
        prompt_user = (
            "I have no relevant documents indexed for this question. "
            "Please respond honestly that no source was found.\n\nQUESTION:\n"
            + question
            + _lang_directive(question)
        )
    messages.append({"role": "user", "content": prompt_user})

    try:
        # temperature 0 → a repeated identical question gives the same grounded
        # answer instead of flip-flopping between "here it is" and "not found".
        answer_text = await client.chat(messages, temperature=0.0, max_tokens=900)
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
        msg = ChatMessage(chat_id=chat_id, role="assistant", content=answer_text, sources=sources)
        session.add(msg)

    return RAGResult(answer=answer_text, citations=citations)


async def stream_answer(
    *,
    chat_id: int,
    user: User,
    question: str,
    top_k: int = 15,
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
        overview_block = _library_overview(user, filters, session)
        history = session.exec(
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.id.desc())
            .limit(history_window)
        ).all()
        history.reverse()
        session.add(ChatMessage(chat_id=chat_id, role="user", content=question))
        from datetime import datetime

        chat.updated_at = datetime.now(UTC)
        session.add(chat)

    eff_top_k, max_per_doc = _retrieval_plan(question, top_k)
    client = get_client()
    hits, ctx_tokens = await asyncio.gather(
        hybrid_search(
            question,
            top_k=eff_top_k,
            document_ids=filters.get("document_ids"),
            source_ids=filters.get("source_ids"),
            tags=filters.get("tags"),
            user=user,
        ),
        client.model_context_length(),
    )
    context_block, citations = _build_context_block(
        hits,
        max_chars=context_char_budget(ctx_tokens),
        max_per_doc=max_per_doc,
    )

    sys = SYSTEM_PROMPT
    if overview_block:
        sys += "\n\n" + overview_block
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
                "content": "SOURCES:\n"
                + context_block
                + "\n\nQUESTION:\n"
                + question
                + "\n\nAnswer using the SOURCES above. Cite with [#]. The SOURCES are "
                "authoritative for THIS question and supersede anything stated in earlier turns."
                + _lang_directive(question),
            }
        )
    else:
        messages.append(
            {
                "role": "user",
                "content": "No documents are indexed for this question. Be honest about it.\n\n"
                + question
                + _lang_directive(question),
            }
        )

    yield {
        "type": "sources",
        "citations": [c.__dict__ for c in citations],
    }

    full: list[str] = []
    try:
        async for piece in client.chat_stream(messages, temperature=0.0, max_tokens=900):
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

    # Streaming produced nothing (no error raised) — e.g. a server/model whose
    # streamed deltas we couldn't parse. Fall back to one non-streamed completion
    # so the user still gets an answer instead of an empty bubble.
    if not full:
        logger.info("rag: streaming yielded no content — trying non-stream fallback")
        try:
            # Bound it: client.chat() retries up to 3× and each attempt can wait
            # out a long CPU timeout, so cap the whole fallback so it can't hang
            # for minutes after the user already saw streaming finish.
            answer = await asyncio.wait_for(
                client.chat(messages, temperature=0.0, max_tokens=900), timeout=60
            )
        except Exception as e:
            logger.warning("rag non-stream fallback failed: {}", e)
            answer = ""
        if answer.strip():
            full.append(answer)
            yield {"type": "token", "text": answer}
        else:
            logger.warning("rag: non-stream fallback also returned no answer")

    answer_text = "".join(full)
    with session_scope() as session:
        sources = [c.__dict__ for c in citations]
        session.add(ChatMessage(chat_id=chat_id, role="assistant", content=answer_text, sources=sources))

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
