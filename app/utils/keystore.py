"""Master secret-key storage — kept OUT of settings.json (and out of backups).

The app's ``secret_key`` derives the credential-store encryption key, the
session-cookie signer and the media-token signer. Storing it in plaintext
``settings.json`` (which is included in backups) made the credential store
reversible by anyone with the data folder or a shared backup.

This keeps the SAME key value and all derivations (so existing encrypted secrets
and sessions keep working), but moves it to a dedicated ``secret.key`` file that
is never part of a backup. On Windows the file is wrapped with **DPAPI**
(``CryptProtectData``, user-scoped), so a copied data folder is useless on
another machine or account. Elsewhere it falls back to a restricted-permission
plaintext file (still strictly better than living in an exported settings.json).
"""

from __future__ import annotations

import base64
import ctypes
import sys
from pathlib import Path

from app.utils.logging import logger

_DPAPI = sys.platform == "win32"


def _dpapi(data: bytes, *, unprotect: bool) -> bytes | None:
    """Round-trip ``data`` through Windows DPAPI (user scope). None on failure."""
    if not _DPAPI:
        return None
    try:
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        fn = ctypes.windll.crypt32.CryptUnprotectData if unprotect else ctypes.windll.crypt32.CryptProtectData
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(out))
        if not ok:
            return None
        result = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return result
    except Exception as e:  # pragma: no cover - platform/edge specific
        logger.debug("DPAPI {} failed: {}", "unprotect" if unprotect else "protect", e)
        return None


def read_secret_key(path: Path) -> str | None:
    """Read the master key from ``path`` (DPAPI-unwrapped on Windows)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        tag, _, body = raw.partition(b":")
        if tag == b"DPAPI":
            data = _dpapi(base64.b64decode(body), unprotect=True)
            return data.decode("utf-8") if data else None
        if tag == b"PLAIN":
            return body.decode("utf-8")
    except Exception as e:
        logger.warning("secret.key unreadable ({}); a new key will be generated", e)
    return None


def write_secret_key(path: Path, key: str) -> None:
    """Persist the master key to ``path`` (DPAPI-wrapped on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = key.encode("utf-8")
    protected = _dpapi(data, unprotect=False)
    blob = (b"DPAPI:" + base64.b64encode(protected)) if protected is not None else (b"PLAIN:" + data)
    path.write_bytes(blob)
    try:
        path.chmod(0o600)
    except OSError:
        pass
