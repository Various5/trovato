"""Filesystem scanner — walks a source directory and yields PDF candidates."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from app.models import DocumentSource


@dataclass(slots=True)
class DiscoveredFile:
    path: Path
    size_bytes: int
    mtime: float


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def discover_files(source: DocumentSource) -> Iterator[DiscoveredFile]:
    """Walk the configured source directory and yield matching files."""
    root = Path(source.path)
    if not root.exists() or not root.is_dir():
        return

    include = source.include_patterns or ["*.pdf"]
    exclude = source.exclude_patterns or []
    max_bytes = (source.max_file_size_mb or 0) * 1024 * 1024

    walker = root.rglob("*") if source.recursive else root.iterdir()
    for p in walker:
        if not p.is_file():
            continue
        if source.ignore_hidden and any(part.startswith(".") for part in p.parts):
            continue
        if not _matches_any(p.name, include):
            continue
        if exclude and _matches_any(p.name, exclude):
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        if max_bytes and stat.st_size > max_bytes:
            continue
        yield DiscoveredFile(path=p, size_bytes=stat.st_size, mtime=stat.st_mtime)
