"""SFTP provider via paramiko (optional dep).

``credentials_ref`` payload: ``{"host": "...", "port": 22, "username": "...",
"password": "..."}`` or ``"private_key_path": "..."``.
"""

from __future__ import annotations

import fnmatch
import stat as stat_mod
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings
from app.ingestion.providers.base import LocalisedFile
from app.models import DocumentSource
from app.utils.logging import logger
from app.utils.paths import safe_filename
from app.utils.secret_store import get_secret


class SFTPProvider:
    def iter_files(self, source: DocumentSource) -> Iterator[LocalisedFile]:
        try:
            import paramiko  # type: ignore
        except ImportError:
            logger.warning("paramiko not installed — `pip install paramiko` to use SFTP sources")
            return

        creds = get_secret(source.credentials_ref) if source.credentials_ref else None
        if not creds:
            logger.warning("SFTP source {} has no credentials", source.id)
            return

        host = creds.get("host")
        port = int(creds.get("port", 22))
        user = creds.get("username")
        pw = creds.get("password")
        key_path = creds.get("private_key_path")
        if not host or not user:
            logger.warning("SFTP credentials incomplete for source {}", source.id)
            return

        try:
            transport = paramiko.Transport((host, port))
            if key_path:
                pkey = paramiko.RSAKey.from_private_key_file(key_path, password=pw or None)
                transport.connect(username=user, pkey=pkey)
            else:
                transport.connect(username=user, password=pw)
            sftp = paramiko.SFTPClient.from_transport(transport)
        except Exception as e:
            logger.exception("SFTP connect failed for source {}: {}", source.id, e)
            return

        cache_root = get_settings().cache_path / "remote" / str(source.id)
        cache_root.mkdir(parents=True, exist_ok=True)

        include = source.include_patterns or ["*.pdf"]
        exclude = source.exclude_patterns or []
        max_bytes = (source.max_file_size_mb or 0) * 1024 * 1024
        try:
            yield from self._walk(
                sftp,
                source.path or ".",
                cache_root,
                include,
                exclude,
                max_bytes,
                recursive=source.recursive,
            )
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass

    def _walk(
        self,
        sftp,
        remote_dir: str,
        cache_root: Path,
        include: list[str],
        exclude: list[str],
        max_bytes: int,
        *,
        recursive: bool,
    ) -> Iterator[LocalisedFile]:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except Exception as e:
            logger.debug("SFTP listdir failed for {}: {}", remote_dir, e)
            return

        for attr in entries:
            name = attr.filename
            if name in (".", ".."):
                continue
            full = remote_dir.rstrip("/") + "/" + name
            if stat_mod.S_ISDIR(attr.st_mode):
                if recursive:
                    yield from self._walk(
                        sftp,
                        full,
                        cache_root,
                        include,
                        exclude,
                        max_bytes,
                        recursive=recursive,
                    )
                continue

            if not any(fnmatch.fnmatch(name, pat) for pat in include):
                continue
            if exclude and any(fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            size = int(attr.st_size or 0)
            if max_bytes and size > max_bytes:
                continue

            local = cache_root / safe_filename(full.lstrip("/"))
            local.parent.mkdir(parents=True, exist_ok=True)
            sig_file = local.with_suffix(local.suffix + ".sig")
            sig = f"{size}:{attr.st_mtime or 0}"
            need_download = not local.exists() or not sig_file.exists() or sig_file.read_text() != sig
            if need_download:
                try:
                    sftp.get(full, str(local))
                    sig_file.write_text(sig, encoding="utf-8")
                except Exception as e:
                    logger.warning("SFTP get failed {}: {}", full, e)
                    continue
            try:
                st = local.stat()
            except OSError:
                continue
            yield LocalisedFile(
                local_path=local,
                remote_path=full,
                size_bytes=st.st_size,
                mtime=st.st_mtime,
            )
