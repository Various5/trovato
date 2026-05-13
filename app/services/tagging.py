"""Lightweight automatic tagging using heuristics + LM Studio classifier prompt.

Heuristic-only by default so indexing works even if the LLM is offline.
A future revision can call the chat model for richer classification.
"""

from __future__ import annotations

import re
from collections import Counter

_DOC_TYPE_KEYWORDS = {
    "rechnung": ["rechnung", "invoice", "rechnungsnummer", "tax invoice", "betrag", "ust-id"],
    "vertrag": ["vertrag", "agreement", "contract", "vereinbarung", "parties"],
    "anleitung": ["anleitung", "manual", "handbuch", "user guide", "instructions"],
    "bericht": ["report", "bericht", "executive summary", "findings"],
    "formular": ["formular", "form", "applicant", "antragsteller"],
    "scan": ["scanned", "scanner", "ocr"],
    "notiz": ["notiz", "memo", "note"],
    "praesentation": ["slide", "präsentation", "powerpoint"],
}

_SENSITIVE_PATTERNS = [
    ("personenbezogene_daten", re.compile(r"\b(geburtsdatum|date of birth|iban|bic|tax id)\b", re.I)),
    ("zugangsdaten", re.compile(r"\b(password|passwort|api[_ -]?key|secret)\b", re.I)),
    ("finanzen", re.compile(r"\b(betrag|total|amount|sum|eur|usd)\b", re.I)),
    ("medizin", re.compile(r"\b(diagnose|patient|medication|therapy)\b", re.I)),
]


def detect_language(text: str) -> str | None:
    """Very small heuristic — distinguishes German from English."""
    if not text:
        return None
    german_markers = sum(text.lower().count(w) for w in (" und ", " der ", " die ", " ist ", "ß"))
    english_markers = sum(text.lower().count(w) for w in (" the ", " and ", " is ", " of "))
    if german_markers > english_markers:
        return "de"
    if english_markers > 0:
        return "en"
    return None


def detect_doc_type(text: str) -> str | None:
    if not text:
        return None
    t = text.lower()
    scores = Counter()
    for doc_type, words in _DOC_TYPE_KEYWORDS.items():
        for w in words:
            scores[doc_type] += t.count(w)
    if not scores:
        return None
    top, score = scores.most_common(1)[0]
    return top if score > 0 else None


def detect_sensitivity_tags(text: str) -> list[str]:
    if not text:
        return []
    tags: list[str] = []
    for tag, pat in _SENSITIVE_PATTERNS:
        if pat.search(text):
            tags.append(tag)
    return tags


_DATE_RX = re.compile(
    r"\b("
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4}"
    r")\b",
    re.I,
)
_AMOUNT_RX = re.compile(
    r"(?:[€$£¥]\s*\d[\d.,]*|\b\d[\d.,]+\s?(?:EUR|USD|GBP|CHF))",
    re.I,
)
_ORG_RX = re.compile(
    r"\b([A-Z][A-Za-z0-9&]+(?:\s+[A-Z][A-Za-z0-9&]+){0,3}\s+(?:GmbH|AG|SE|KG|Ltd|Inc|LLC|S\.A\.|S\.p\.A\.|Holding))\b"
)
_EMAIL_RX = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def extract_entities(text: str) -> dict[str, list[str]]:
    """Pull dates, amounts, organisations, e-mails out of the text (heuristic)."""
    return {
        "dates": list({m.group(0).strip() for m in _DATE_RX.finditer(text)})[:20],
        "amounts": list({m.group(0).strip() for m in _AMOUNT_RX.finditer(text)})[:20],
        "organisations": list({m.group(1).strip() for m in _ORG_RX.finditer(text)})[:20],
        "emails": list({m.group(0).strip() for m in _EMAIL_RX.finditer(text)})[:10],
    }


def auto_tags(text: str, *, vision_text: str = "") -> list[str]:
    """Build a tag list from text + optional vision-description text.

    Adds: doc-type, language, sensitivity, and high-level flags like
    ``has:dates``, ``has:amounts``, ``has:org``.
    """
    tags: list[str] = []
    haystack = text or ""
    if vision_text:
        haystack = haystack + "\n" + vision_text
    dt = detect_doc_type(haystack)
    if dt:
        tags.append(dt)
    lang = detect_language(haystack)
    if lang:
        tags.append(f"lang:{lang}")
    tags.extend(detect_sensitivity_tags(haystack))

    ent = extract_entities(haystack)
    if ent["dates"]:
        tags.append("has:dates")
    if ent["amounts"]:
        tags.append("has:amounts")
    if ent["organisations"]:
        tags.append("has:org")
    if vision_text:
        tags.append("has:images")
    return sorted(set(tags))
