"""Provider base class + dispatcher."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
        # With credentials → talk to the share directly via smbprotocol.
        # Without credentials → assume a pre-mounted drive / cifs mount and
        # use the local provider.
        if source.credentials_ref:
            from app.ingestion.providers.smb import SMBProvider

            return SMBProvider()
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
