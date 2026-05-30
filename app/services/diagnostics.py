"""Health diagnostics — disk usage, duplicates, orphan caches, LM Studio."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlmodel import select

from app.config import get_settings
from app.database import session_scope
from app.llm import LMStudioClient
from app.models import Document, DocumentChunk, DocumentImage
from app.vectorstore import collection_size


def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                continue
    return total


def storage_overview() -> dict[str, Any]:
    s = get_settings()
    items = {
        "database": s.db_path,
        "vector_store": s.chroma_path,
        "cache_pages": s.cache_path / "pages",
        "cache_images": s.cache_path / "images",
        "backups": s.backups_path,
        "logs": s.logs_path,
    }
    out: dict[str, Any] = {}
    for name, p in items.items():
        size = p.stat().st_size if p.exists() and p.is_file() else _dir_size_bytes(p)
        out[name] = {"path": str(p), "size_bytes": size, "exists": p.exists()}
    return out


def find_duplicates() -> list[dict[str, Any]]:
    """Return groups of documents that share the same content_hash."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with session_scope() as session:
        for d in session.exec(select(Document)).all():
            groups[d.content_hash].append(
                {"id": d.id, "filename": d.filename, "path": d.path, "size_bytes": d.size_bytes}
            )
    return [{"content_hash": h, "documents": v} for h, v in groups.items() if len(v) > 1]


def orphan_caches() -> dict[str, Any]:
    """Cache folders for document IDs that no longer exist in the DB."""
    s = get_settings()
    out: dict[str, list[str]] = {"pages": [], "images": []}
    with session_scope() as session:
        live_ids = {d.id for d in session.exec(select(Document)).all()}
    for kind, root in (("pages", s.cache_path / "pages"), ("images", s.cache_path / "images")):
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                cid = int(child.name)
            except ValueError:
                continue
            if cid not in live_ids:
                out[kind].append(str(child))
    return out


def cleanup_orphan_caches() -> dict[str, int]:
    """Delete orphan cache dirs — returns counts."""
    import shutil

    found = orphan_caches()
    counts = {"pages": 0, "images": 0}
    for kind, dirs in found.items():
        for d in dirs:
            try:
                shutil.rmtree(d, ignore_errors=True)
                counts[kind] += 1
            except OSError:
                continue
    return counts


def index_overview() -> dict[str, Any]:
    with session_scope() as session:
        doc_count = len(session.exec(select(Document)).all())
        chunk_count = len(session.exec(select(DocumentChunk)).all())
        image_count = len(session.exec(select(DocumentImage)).all())
    return {
        "documents": doc_count,
        "chunks": chunk_count,
        "images": image_count,
        "vector_count": collection_size(),
    }


async def lmstudio_status() -> dict[str, Any]:
    c = LMStudioClient()
    try:
        ok = await asyncio.wait_for(c.ping(), timeout=4.0)
    except TimeoutError:
        ok = False
    out: dict[str, Any] = {"base_url": c.base_url, "reachable": ok}
    if ok:
        try:
            models = await asyncio.wait_for(c.list_models(), timeout=6.0)
            out["models"] = [m.get("id") for m in models]
        except Exception as e:
            out["models_error"] = str(e)
    return out
