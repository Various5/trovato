"""Compare two documents — structural diff + LLM-narrated summary."""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from sqlmodel import select

from app.database import session_scope
from app.llm import LMStudioError, get_client
from app.models import Document, DocumentChunk


@dataclass
class CompareResult:
    doc_a: dict
    doc_b: dict
    only_in_a_sample: list[str]
    only_in_b_sample: list[str]
    shared_ratio: float
    narrative: str


def _aggregate_text(document_id: int, char_limit: int = 12_000) -> tuple[dict, str]:
    with session_scope() as session:
        doc = session.get(Document, document_id)
        if not doc:
            return {}, ""
        chunks = session.exec(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.page_from)
        ).all()
        text = "\n".join(c.text for c in chunks)[:char_limit]
        meta = {
            "id": doc.id,
            "filename": doc.filename,
            "page_count": doc.page_count,
            "doc_type": doc.doc_type,
            "language": doc.language,
        }
        return meta, text


def _line_diff(a: str, b: str, max_examples: int = 12) -> tuple[list[str], list[str], float]:
    a_lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
    b_lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
    matcher = difflib.SequenceMatcher(None, a_lines, b_lines, autojunk=False)
    ratio = matcher.ratio()

    set_a = set(a_lines)
    set_b = set(b_lines)
    only_a = [ln for ln in a_lines if ln not in set_b][:max_examples]
    only_b = [ln for ln in b_lines if ln not in set_a][:max_examples]
    return only_a, only_b, ratio


async def compare_documents(doc_a: int, doc_b: int) -> CompareResult:
    meta_a, text_a = _aggregate_text(doc_a)
    meta_b, text_b = _aggregate_text(doc_b)
    if not meta_a or not meta_b:
        raise ValueError("one or both documents not found")

    only_a, only_b, ratio = _line_diff(text_a, text_b)

    prompt = (
        "You are comparing two documents. Provide a structured comparison covering:\n"
        "1. Type and purpose of each document.\n"
        "2. Key shared topics.\n"
        "3. Key differences (facts, parties, dates, financial figures).\n"
        "4. Recommendation: which is more recent / authoritative if discernible.\n\n"
        f"DOC A: {meta_a.get('filename')} (p.{meta_a.get('page_count')})\n{text_a}\n\n"
        f"DOC B: {meta_b.get('filename')} (p.{meta_b.get('page_count')})\n{text_b}\n\n"
        "Keep the answer concise; cite specific phrases when relevant."
    )

    client = get_client()
    try:
        narrative = await client.chat(
            [{"role": "user", "content": prompt}], temperature=0.1, max_tokens=900
        )
    except LMStudioError as e:
        narrative = f"_LM Studio unavailable: {e}_"
    except Exception as e:
        narrative = f"_Comparison failed: {e}_"

    return CompareResult(
        doc_a=meta_a,
        doc_b=meta_b,
        only_in_a_sample=only_a,
        only_in_b_sample=only_b,
        shared_ratio=ratio,
        narrative=narrative,
    )
