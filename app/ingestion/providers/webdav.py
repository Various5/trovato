"""WebDAV provider via the ``webdavclient3`` package (optional dep).

The source's ``path`` is the *remote* root, e.g. ``/Documents/PDFs/``. The
``credentials_ref`` points to an entry in the encrypted secret store
containing ``base_url``, ``username``, ``password``.

Files are mirrored into ``<cache>/remote/<source_id>/...`` on each scan; only
when the remote ``etag``/``modification time``/``size`` changes is the file
re-downloaded.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.config import get_settings
from app.ingestion.providers.base import LocalisedFile
from app.models import DocumentSource
from app.utils.logging import logger
from app.utils.paths import safe_filename
from app.utils.secret_store import get_secret


class WebDAVProvider:
    def iter_files(self, source: DocumentSource) -> Iterator[LocalisedFile]:
        try:
            from webdav3.client import Client as WebDAVClient  # type: ignore
        except ImportError:
            logger.warning(
                "webdavclient3 not installed — `pip install webdavclient3` to use WebDAV sources"
            )
            return

        creds = get_secret(source.credentials_ref) if source.credentials_ref else None
        if not creds:
            logger.warning("WebDAV source {} has no credentials", source.id)
            return

        opts = {
            "webdav_hostname": creds.get("base_url"),
            "webdav_login": creds.get("username"),
            "webdav_password": creds.get("password"),
        }
        try:
            client = WebDAVClient(opts)
        except Exception as e:
            logger.error("WebDAV client init failed: {}", e)
            return

        include = source.include_patterns or ["*.pdf"]
        exclude = source.exclude_patterns or []
        max_bytes = (source.max_file_size_mb or 0) * 1024 * 1024

        cache_root = get_settings().cache_path / "remote" / str(source.id)
        cache_root.mkdir(parents=True, exist_ok=True)

        remote_root = source.path or "/"
        try:
            yield from self._walk(
                client,
                remote_root,
                cache_root,
                include,
                exclude,
                max_bytes,
                recursive=source.recursive,
            )
        except Exception as e:
            logger.exception("WebDAV walk failed for source {}: {}", source.id, e)

    def _walk(
        self,
        client,
        remote_dir: str,
        cache_root: Path,
        include: list[str],
        exclude: list[str],
        max_bytes: int,
        *,
        recursive: bool,
    ) -> Iterator[LocalisedFile]:
        try:
            entries = client.list(remote_dir, get_info=True)
        except Exception as e:
            logger.debug("WebDAV list failed for {}: {}", remote_dir, e)
            return

        for info in entries:
            path = info.get("path") or info.get("name")
            if not path:
                continue
            if info.get("isdir"):
                if recursive and PurePosixPath(path).as_posix().rstrip("/") not in (
                    PurePosixPath(remote_dir).as_posix().rstrip("/"),
                ):
                    yield from self._walk(
                        client, path, cache_root, include, exclude, max_bytes,
                        recursive=recursive,
                    )
                continue

            name = PurePosixPath(path).name
            if not any(fnmatch.fnmatch(name, pat) for pat in include):
                continue
            if exclude and any(fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            try:
                size = int(info.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if max_bytes and size > max_bytes:
                continue

            local = cache_root / safe_filename(PurePosixPath(path).as_posix().lstrip("/"))
            local.parent.mkdir(parents=True, exist_ok=True)

            mtime_marker = info.get("modified") or info.get("etag") or ""
            sig_file = local.with_suffix(local.suffix + ".sig")
            sig = f"{size}:{mtime_marker}"
            need_download = not local.exists() or not sig_file.exists() or sig_file.read_text() != sig
            if need_download:
                try:
                    client.download_sync(remote_path=path, local_path=str(local))
                    sig_file.write_text(sig, encoding="utf-8")
                except Exception as e:
                    logger.warning("WebDAV download failed {}: {}", path, e)
                    continue

            try:
                st = local.stat()
            except OSError:
                continue
            yield LocalisedFile(
                local_path=local,
                remote_path=path,
                size_bytes=st.st_size,
                mtime=st.st_mtime,
            )
