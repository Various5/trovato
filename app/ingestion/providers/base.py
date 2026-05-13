"""Provider base class + dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from app.models import DocumentSource, SourceType


@dataclass(slots=True)
class LocalisedFile:
    """A file ready for ingestion — always backed by a local path on disk."""

    local_path: Path
    remote_path: str  # for display / dedup; equals local_path for local sources
    size_bytes: int
    mtime: float


class Provider(Protocol):
    def iter_files(self, source: DocumentSource) -> Iterator[LocalisedFile]: ...


def get_provider(source: DocumentSource) -> Provider:
    t = source.type
    if t in (SourceType.local, SourceType.usb, SourceType.cloud_sync):
        from app.ingestion.providers.local import LocalProvider

        return LocalProvider()
    if t == SourceType.smb:
        # SMB on Windows usually appears as a regular mount (\\host\share);
        # treat it as local. On POSIX you'd mount via cifs first.
        from app.ingestion.providers.local import LocalProvider

        return LocalProvider()
    if t == SourceType.webdav:
        from app.ingestion.providers.webdav import WebDAVProvider

        return WebDAVProvider()
    if t == SourceType.sftp:
        from app.ingestion.providers.sftp import SFTPProvider

        return SFTPProvider()
    # other / unknown → local
    from app.ingestion.providers.local import LocalProvider

    return LocalProvider()
