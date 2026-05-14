"""Suggested chat starters — picked from what's actually indexed so the user
can hit the ground running on a fresh chat."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from app.database import session_scope
from app.models import Document, DocumentSource

_TYPE_TEMPLATES = {
    "rechnung": [
        "Welche Rechnungsbeträge sind im aktuellen Jahr angefallen?",
        "Liste alle offenen Rechnungen mit Fälligkeit.",
        "Vergleiche die Rechnungen aus diesem und dem letzten Quartal.",
    ],
    "vertrag": [
        "Welche Verträge laufen in den nächsten 90 Tagen aus?",
        "Fasse die wichtigsten Klauseln meiner aktiven Verträge zusammen.",
        "Wer sind die Vertragspartner und welche Kündigungsfristen gelten?",
    ],
    "bericht": [
        "Was sind die zentralen Ergebnisse der letzten Berichte?",
        "Vergleiche die letzten zwei Quartalsberichte.",
    ],
    "anleitung": [
        "Gib mir eine Schritt-für-Schritt-Übersicht aus den Anleitungen.",
    ],
    "formular": [
        "Welche Formulare benötigen meine Unterschrift?",
    ],
    "notiz": [
        "Sammle die offenen Punkte aus meinen Notizen.",
    ],
    "scan": [
        "Welche Scans enthalten Text zu …",
    ],
    "praesentation": [
        "Fasse die Hauptaussagen der Präsentationen zusammen.",
    ],
}


_GENERIC = [
    "Worum geht es in den zuletzt hinzugefügten Dokumenten?",
    "Gibt es Dokumente, die ähnliche Themen behandeln?",
    "Finde alle Dokumente, die Personen oder Organisationen erwähnen.",
    "Welche Dokumente sind älter als ein Jahr?",
    "Gib mir einen Überblick über alle Dokumente in dieser Bibliothek.",
]


def suggested_starters(limit: int = 6) -> list[dict[str, str]]:
    """Return ``[{question, hint}, …]`` — content-aware starters for the chat
    landing page. Picks from templates keyed on document types found in the
    index, plus a few generic prompts so an empty index still has suggestions.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    with session_scope() as session:
        docs = session.exec(select(Document)).all()
        doc_types = sorted({d.doc_type for d in docs if d.doc_type})
        total = len(docs)
        sources = session.exec(select(DocumentSource)).all()
        source_count = len(sources)

    for dt in doc_types:
        for tmpl in _TYPE_TEMPLATES.get(dt, []):
            if tmpl in seen:
                continue
            seen.add(tmpl)
            out.append({"question": tmpl, "hint": f"{dt}"})
            if len(out) >= limit:
                return out

    for g in _GENERIC:
        if g in seen:
            continue
        seen.add(g)
        hint = f"{total} docs · {source_count} sources" if total else "Add a source to get started"
        out.append({"question": g, "hint": hint})
        if len(out) >= limit:
            break
    return out


_ = datetime, timezone  # silence unused imports
