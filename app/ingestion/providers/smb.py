"""SMB provider via ``smbprotocol`` (optional dep).

``credentials_ref`` payload:
    {
      "server":   "fileserver.local",   # or IP
      "domain":   "WORKGROUP",          # optional
      "username": "alice",
      "password": "..."
    }

The source's ``path`` is either the UNC path ``\\\\server\\share\\sub\\dir``
or just ``share/sub/dir`` (server is then taken from credentials).
Files are downloaded into ``<cache>/remote/<source_id>/...`` on demand and
re-used as long as size + mtime match.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from pathlib import Path

from app.config import get_settings
from app.ingestion.providers.base import LocalisedFile
from app.models import DocumentSource
from app.utils.logging import logger
from app.utils.paths import safe_filename
from app.utils.secret_store import get_secret


def _split_unc(path: str) -> tuple[str, str]:
    """Split a UNC-ish path into ``(server, sharepath)``.

    Accepts ``\\\\server\\share\\sub``, ``//server/share/sub`` and bare
    ``share/sub`` (in which case server is "" and the caller supplies one).
    """
    p = path.replace("\\", "/").lstrip("/")
    parts = [seg for seg in p.split("/") if seg]
    if path.startswith(("\\\\", "//")) and len(parts) >= 1:
        return parts[0], "/".join(parts[1:])
    return "", "/".join(parts)


class SMBProvider:
    def iter_files(self, source: DocumentSource) -> Iterator[LocalisedFile]:
        try:
            import smbclient
        except ImportError:
            logger.warning("smbprotocol not installed — `pip install smbprotocol` to use SMB sources")
            return

        creds = get_secret(source.credentials_ref) if source.credentials_ref else None
        if not creds:
            logger.warning("SMB source {} has no credentials", source.id)
            return

        server_from_path, share_path = _split_unc(source.path or "")
        server = creds.get("server") or server_from_path
        if not server:
            logger.warning(
                "SMB source {} has no server (set in credentials or use \\\\server\\share path)",
                source.id,
            )
            return

        username = creds.get("username") or ""
        domain = creds.get("domain") or ""
        password = creds.get("password") or ""
        full_user = f"{domain}\\{username}" if domain and username else username

        try:
            smbclient.register_session(
                server=server,
                username=full_user or None,
                password=password or None,
                connection_timeout=12,
            )
        except Exception as e:
            logger.exception("SMB register_session failed for {}: {}", server, e)
            return

        include = source.include_patterns or ["*.pdf"]
        exclude = source.exclude_patterns or []
        max_bytes = (source.max_file_size_mb or 0) * 1024 * 1024
        cache_root = get_settings().cache_path / "remote" / str(source.id)
        cache_root.mkdir(parents=True, exist_ok=True)

        unc_root = f"\\\\{server}\\{share_path.replace('/', chr(92))}".rstrip("\\")
        try:
            yield from self._walk(
                smbclient,
                unc_root,
                cache_root,
                include,
                exclude,
                max_bytes,
                recursive=source.recursive,
            )
        except Exception as e:
            logger.exception("SMB walk failed for source {}: {}", source.id, e)
        finally:
            try:
                smbclient.delete_session(server)
            except Exception:
                pass

    def _walk(
        self,
        smbclient,
        unc_dir: str,
        cache_root: Path,
        include: list[str],
        exclude: list[str],
        max_bytes: int,
        *,
        recursive: bool,
    ) -> Iterator[LocalisedFile]:
        try:
            entries = list(smbclient.scandir(unc_dir))
        except Exception as e:
            logger.debug("SMB scandir failed for {}: {}", unc_dir, e)
            return

        for entry in entries:
            name = entry.name
            if name in (".", ".."):
                continue
            full_unc = f"{unc_dir}\\{name}"
            try:
                is_dir = entry.is_dir()
                stat_info = entry.stat()
            except Exception:
                continue

            if is_dir:
                if recursive:
                    yield from self._walk(
                        smbclient,
                        full_unc,
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
            size = int(stat_info.st_size or 0)
            mtime = float(stat_info.st_mtime or 0)
            if max_bytes and size > max_bytes:
                continue

            # Mirror to cache; only download when size/mtime changes.
            rel = full_unc.lstrip("\\")
            local = cache_root / safe_filename(rel)
            local.parent.mkdir(parents=True, exist_ok=True)
            sig_file = local.with_suffix(local.suffix + ".sig")
            sig = f"{size}:{int(mtime)}"
            need_download = (
                not local.exists() or not sig_file.exists() or sig_file.read_text(encoding="utf-8") != sig
            )
            if need_download:
                try:
                    with smbclient.open_file(full_unc, mode="rb") as src, local.open("wb") as dst:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            dst.write(chunk)
                    sig_file.write_text(sig, encoding="utf-8")
                except Exception as e:
                    logger.warning("SMB download failed {}: {}", full_unc, e)
                    continue

            try:
                st = local.stat()
            except OSError:
                continue
            yield LocalisedFile(
                local_path=local,
                remote_path=full_unc,
                size_bytes=st.st_size,
                mtime=st.st_mtime,
            )
