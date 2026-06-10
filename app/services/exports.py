"""Export helpers — chats to Markdown, search hits to CSV/JSON."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from typing import Any

from sqlmodel import select

from app.database import session_scope
from app.models import Chat, ChatMessage
from app.services.search_service import SearchHit


def chat_to_pdf(chat_id: int) -> bytes:
    """Render a chat to PDF using PyMuPDF (one column, plain text layout)."""
    import fitz  # PyMuPDF

    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat:
            raise ValueError("chat not found")
        msgs = session.exec(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)
        ).all()
        title = chat.title or "Untitled chat"
        msg_data = [(m.role, m.content, list(m.sources or [])) for m in msgs]

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait, points
    cursor_y = 60
    margin_x = 56
    text_width = 595 - 2 * margin_x

    def _ensure_space(needed: float) -> None:
        nonlocal page, cursor_y
        if cursor_y + needed > 800:
            page = doc.new_page(width=595, height=842)
            cursor_y = 60

    def _write(text: str, *, size: int = 11, bold: bool = False, indent: int = 0) -> None:
        nonlocal cursor_y
        if not text:
            return
        font = "Helvetica-Bold" if bold else "Helvetica"
        # Word-wrap manually using fitz' insert_textbox with auto-growth
        rect = fitz.Rect(margin_x + indent, cursor_y, margin_x + text_width, 820)
        # Two-pass: render, measure remaining
        before = cursor_y
        used = page.insert_textbox(rect, text, fontsize=size, fontname=font, align=0)
        # used < 0 means overflow → split into chunks
        if used < 0:
            paragraphs = text.split("\n")
            for para in paragraphs:
                _ensure_space(20)
                rect = fitz.Rect(margin_x + indent, cursor_y, margin_x + text_width, 820)
                page.insert_textbox(rect, para, fontsize=size, fontname=font, align=0)
                cursor_y += max(14, min(60, int(len(para) / 80) * 14 + 14))
            return
        # Approximate vertical advance
        lines = max(
            1,
            int(rect.width and (len(text) / max(60, int(rect.width / (size * 0.55)))) + text.count("\n") + 1),
        )
        cursor_y = before + lines * (size + 4)

    _write(title, size=18, bold=True)
    cursor_y += 6
    _write(f"Exported with Trovato — {len(msg_data)} message(s)", size=9)
    cursor_y += 14

    for role, content, sources in msg_data:
        _ensure_space(40)
        header = "You" if role == "user" else "Assistant" if role == "assistant" else role
        _write(header, size=12, bold=True)
        cursor_y += 4
        _write(content[:8000], size=11)
        cursor_y += 6
        if sources:
            _write("Sources:", size=10, bold=True, indent=10)
            for s in sources:
                _write(
                    f"[{s.get('n')}] {s.get('filename')} (p.{s.get('page_from')})",
                    size=9,
                    indent=22,
                )
        cursor_y += 12

    out = doc.tobytes()
    doc.close()
    return out


def chat_to_markdown(chat_id: int) -> str:
    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat:
            raise ValueError("chat not found")
        msgs = session.exec(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)
        ).all()
        when = chat.updated_at.isoformat() if chat.updated_at else "—"
        lines = [f"# {chat.title or 'Untitled chat'}", "", f"_Exported: {when}_", ""]
        for m in msgs:
            who = (
                "**You**" if m.role == "user" else "**Assistant**" if m.role == "assistant" else f"_{m.role}_"
            )
            lines.append(f"### {who}")
            lines.append("")
            lines.append(m.content)
            lines.append("")
            if m.sources:
                lines.append("**Sources:**")
                for s in m.sources:
                    lines.append(
                        f"- [{s.get('n')}] {s.get('filename')} (p.{s.get('page_from')}) — "
                        f"`{s.get('path')}`"
                    )
                lines.append("")
        return "\n".join(lines)


def search_hits_to_csv(hits: list[SearchHit]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=[
            "chunk_id",
            "document_id",
            "filename",
            "path",
            "page_from",
            "page_to",
            "score",
            "source",
            "snippet",
            "tags",
        ],
    )
    writer.writeheader()
    for h in hits:
        row = asdict(h)
        row["tags"] = ";".join(h.tags or [])
        writer.writerow(row)
    return buf.getvalue()


def search_hits_to_json(hits: list[SearchHit]) -> str:
    return json.dumps([asdict(h) for h in hits], indent=2, ensure_ascii=False)


def search_hits_to_dict_list(hits: list[SearchHit]) -> list[dict[str, Any]]:
    return [asdict(h) for h in hits]
