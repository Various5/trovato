"""Smoke tests for the phased indexing pipeline (v0.3.5).

Validates that ``phase="quick"`` short-circuits and that a later heavier
phase on the same file still runs the full extraction pipeline. The actual
PDF extraction / LM Studio calls are stubbed so the tests can run offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.database import init_db, session_scope
from app.models import Document, DocumentSource, SourceType, Visibility
from app.services import indexer


def _make_source(tmp_path: Path) -> DocumentSource:
    init_db()
    with session_scope() as session:
        s = DocumentSource(
            name="t",
            type=SourceType.local,
            path=str(tmp_path),
            owner_id=None,
            visibility=Visibility.private,
        )
        session.add(s)
        session.flush()
        snap = DocumentSource(**s.model_dump())
    return snap


@pytest.mark.asyncio
async def test_quick_phase_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quick phase records the doc row and never touches the PDF parser."""
    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%not-a-real-pdf")

    called: dict[str, int] = {"extract": 0, "embed": 0}

    def _no_extract(*a: Any, **kw: Any) -> list:
        called["extract"] += 1
        return []

    async def _no_embed(*a: Any, **kw: Any) -> list[list[float]]:
        called["embed"] += 1
        return []

    monkeypatch.setattr(indexer, "extract_pdf", _no_extract)

    class _Client:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await _no_embed(texts)

        async def describe_image(self, p: Any) -> str:
            return ""

        async def preflight_embed(self) -> tuple[bool, str]:
            return True, "ok"

    monkeypatch.setattr(indexer, "get_client", lambda: _Client())

    src = _make_source(tmp_path)
    doc_id = await indexer.index_document(src, pdf, phase="quick")
    assert doc_id is not None
    assert called["extract"] == 0
    assert called["embed"] == 0

    with session_scope() as session:
        d = session.get(Document, doc_id)
        assert d is not None
        assert d.status.value == "indexed"
        assert d.page_count == 0


@pytest.mark.asyncio
async def test_text_phase_after_quick_runs_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subsequent text-phase run on a catalog-only doc must extract."""
    pdf = tmp_path / "fake2.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%not-a-real-pdf")

    src = _make_source(tmp_path)

    class _Client:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 8 for _ in texts]

        async def describe_image(self, p: Any) -> str:
            return ""

        async def preflight_embed(self) -> tuple[bool, str]:
            return True, "ok"

    monkeypatch.setattr(indexer, "get_client", lambda: _Client())

    # Quick pass first
    monkeypatch.setattr(indexer, "extract_pdf", lambda *a, **kw: [])
    quick_id = await indexer.index_document(src, pdf, phase="quick")
    assert quick_id is not None

    # Now the text pass must not be short-circuited by the matching hash:
    calls: dict[str, int] = {"extract": 0}

    def _stub_extract(*a: Any, **kw: Any) -> list:
        calls["extract"] += 1
        return []

    monkeypatch.setattr(indexer, "extract_pdf", _stub_extract)
    text_id = await indexer.index_document(src, pdf, phase="text")
    # Same doc row
    assert text_id == quick_id
    # Extraction was actually attempted this time
    assert calls["extract"] == 1
