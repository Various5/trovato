"""Suggested chat starters — picked from what's actually indexed so the user
can hit the ground running on a fresh chat.

Templates are keyed by language so an English user doesn't get German prompts
(and vice versa); doc-type keys (rechnung/vertrag/…) are the internal
classifier labels and are shared across languages.
"""

from __future__ import annotations

from sqlmodel import select

from app.database import session_scope
from app.models import Document, DocumentSource

_TYPE_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "en": {
        "rechnung": [
            "Which invoice amounts were incurred this year?",
            "List all open invoices with their due dates.",
            "Compare the invoices from this and last quarter.",
        ],
        "vertrag": [
            "Which contracts expire in the next 90 days?",
            "Summarise the key clauses of my active contracts.",
            "Who are the contract parties and what notice periods apply?",
        ],
        "bericht": [
            "What are the key findings of the latest reports?",
            "Compare the last two quarterly reports.",
        ],
        "anleitung": [
            "Give me a step-by-step overview from the manuals.",
        ],
        "formular": [
            "Which forms need my signature?",
        ],
        "notiz": [
            "Collect the open items from my notes.",
        ],
        "scan": [
            "Which scans contain text about …",
        ],
        "praesentation": [
            "Summarise the main points of the presentations.",
        ],
    },
    "de": {
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
    },
}


_GENERIC: dict[str, list[str]] = {
    "en": [
        "What are the most recently added documents about?",
        "Are there documents covering similar topics?",
        "Find all documents that mention people or organisations.",
        "Which documents are older than a year?",
        "Give me an overview of every document in this library.",
    ],
    "de": [
        "Worum geht es in den zuletzt hinzugefügten Dokumenten?",
        "Gibt es Dokumente, die ähnliche Themen behandeln?",
        "Finde alle Dokumente, die Personen oder Organisationen erwähnen.",
        "Welche Dokumente sind älter als ein Jahr?",
        "Gib mir einen Überblick über alle Dokumente in dieser Bibliothek.",
    ],
}

_EMPTY_HINT: dict[str, str] = {
    "en": "Add a source to get started",
    "de": "Quelle hinzufügen, um loszulegen",
}


def suggested_starters(limit: int = 6, lang: str = "en") -> list[dict[str, str]]:
    """Return ``[{question, hint}, …]`` — content-aware starters for the chat
    landing page, localised to ``lang``. Picks from templates keyed on document
    types found in the index, plus a few generic prompts so an empty index still
    has suggestions.
    """
    lang = (lang or "en").lower()
    if lang not in _TYPE_TEMPLATES:
        lang = "en"
    type_templates = _TYPE_TEMPLATES[lang]
    generic = _GENERIC[lang]
    empty_hint = _EMPTY_HINT.get(lang, _EMPTY_HINT["en"])

    out: list[dict[str, str]] = []
    seen: set[str] = set()

    with session_scope() as session:
        docs = session.exec(select(Document)).all()
        doc_types = sorted({d.doc_type for d in docs if d.doc_type})
        total = len(docs)
        sources = session.exec(select(DocumentSource)).all()
        source_count = len(sources)

    for dt in doc_types:
        for tmpl in type_templates.get(dt, []):
            if tmpl in seen:
                continue
            seen.add(tmpl)
            out.append({"question": tmpl, "hint": f"{dt}"})
            if len(out) >= limit:
                return out

    for g in generic:
        if g in seen:
            continue
        seen.add(g)
        hint = f"{total} docs · {source_count} sources" if total else empty_hint
        out.append({"question": g, "hint": hint})
        if len(out) >= limit:
            break
    return out
