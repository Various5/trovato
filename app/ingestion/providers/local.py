"""Local-filesystem provider (reuses the existing scanner)."""

from __future__ import annotations

from collections.abc import Iterator

from app.ingestion.providers.base import LocalisedFile
from app.ingestion.scanner import discover_files
from app.models import DocumentSource


class LocalProvider:
    def iter_files(self, source: DocumentSource) -> Iterator[LocalisedFile]:
        for f in discover_files(source):
            yield LocalisedFile(
                local_path=f.path,
                remote_path=str(f.path),
                size_bytes=f.size_bytes,
                mtime=f.mtime,
            )
