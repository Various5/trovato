"""Source-provider abstraction.

Each provider exposes ``iter_files(source)`` yielding
``LocalisedFile(local_path, remote_path, size, mtime, hash_hint)`` — local-style
files are returned as-is, remote files (WebDAV/SFTP) get mirrored into the
app's cache directory before being yielded.
"""

from __future__ import annotations

from app.ingestion.providers.base import LocalisedFile, Provider, get_provider  # noqa: F401
