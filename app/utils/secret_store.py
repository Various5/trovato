"""Encrypted key/value store for provider credentials.

Encrypts payloads with Fernet using a key derived from the app's session
secret_key. Persisted in the ``app_settings`` table under the key prefix
``secret:<name>``. Never written to log files.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import select

from app.config import get_settings
from app.database import session_scope
from app.models import AppSetting


_PREFIX = "secret:"


def _fernet() -> Fernet:
    s = get_settings()
    # Stretch the session secret into a stable 32-byte key
    digest = hashlib.sha256(s.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def put_secret(name: str, payload: dict[str, Any]) -> str:
    """Store an encrypted JSON payload under ``name`` (overwrites). Returns the
    storage reference that callers can stash in source.credentials_ref."""
    f = _fernet()
    blob = f.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
    key = _PREFIX + name
    with session_scope() as session:
        existing = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if existing:
            existing.value = blob
            session.add(existing)
        else:
            session.add(AppSetting(key=key, value=blob))
    return name


def get_secret(name: str) -> dict[str, Any] | None:
    key = _PREFIX + name
    with session_scope() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if not row:
            return None
        blob = row.value
    try:
        return json.loads(_fernet().decrypt(blob.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError):
        return None


def delete_secret(name: str) -> bool:
    key = _PREFIX + name
    with session_scope() as session:
        row = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
        if not row:
            return False
        session.delete(row)
        return True
