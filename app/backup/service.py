"""Granular backup / restore service.

Produces a ZIP that contains any subset of these components:
    db, vector, chats, memory, settings, logs, cache, originals.

A ``manifest.json`` at the archive root carries metadata. Encryption is
optional and uses ``cryptography.fernet`` with a key derived from the
user-supplied password (PBKDF2-HMAC-SHA256).
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlmodel import select

from app import __version__
from app.config import get_settings
from app.database import session_scope
from app.models import Backup, Chat, ChatMessage, Document, DocumentChunk, UserMemory
from app.utils.logging import logger
from app.utils.paths import safe_filename

BACKUP_COMPONENTS = ["db", "vector", "chats", "memory", "settings", "logs", "cache", "originals"]


@dataclass
class BackupResult:
    path: Path
    size_bytes: int
    components: list[str]
    encrypted: bool


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _zip_dir(zf: zipfile.ZipFile, src: Path, arcprefix: str) -> int:
    count = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            full = Path(root) / f
            rel = full.relative_to(src)
            zf.write(full, arcname=str(Path(arcprefix) / rel))
            count += 1
    return count


def create_backup(
    components: Iterable[str],
    *,
    output_path: Path | None = None,
    encrypt_password: str | None = None,
    include_originals: bool = False,
) -> BackupResult:
    s = get_settings()
    s.ensure_dirs()
    components = set(components)
    if not components.issubset(set(BACKUP_COMPONENTS)):
        bad = components - set(BACKUP_COMPONENTS)
        raise ValueError(f"unknown components: {bad}")
    if include_originals:
        components.add("originals")

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_name = safe_filename(f"localdoc-backup-{ts}.zip")
    output_path = output_path or (s.backups_path / archive_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # When encryption is requested, we build the ZIP in memory and encrypt the
    # bytes afterwards; otherwise we stream it straight to disk via the path
    # (lets zipfile manage the file handle).
    buf: io.BytesIO | None = io.BytesIO() if encrypt_password else None
    sink: Any = buf if buf is not None else output_path

    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest: dict[str, Any] = {
            "app_version": __version__,
            "created_at": ts,
            "components": sorted(components),
            "encrypted": bool(encrypt_password),
        }

        # DB (snapshot via shutil; SQLite WAL safe enough for offline tools)
        if "db" in components and s.db_path.exists():
            zf.write(s.db_path, arcname="db/localdoc.db")

        # Vector store
        if "vector" in components and s.chroma_path.exists():
            _zip_dir(zf, s.chroma_path, "vector")

        # Chats + memory + settings: export as JSON for portability
        with session_scope() as session:
            doc_count = session.exec(select(Document)).all()
            manifest["document_count"] = len(doc_count)
            chunk_count = session.exec(select(DocumentChunk)).all()
            manifest["chunk_count"] = len(chunk_count)

            if "chats" in components:
                chats = session.exec(select(Chat)).all()
                msgs = session.exec(select(ChatMessage)).all()
                manifest["chat_count"] = len(chats)
                payload = {
                    "chats": [c.model_dump(mode="json") for c in chats],
                    "messages": [m.model_dump(mode="json") for m in msgs],
                }
                zf.writestr(
                    "chats/chats.json", json.dumps(payload, indent=2, default=str, ensure_ascii=False)
                )
            if "memory" in components:
                mems = session.exec(select(UserMemory)).all()
                zf.writestr(
                    "memory/memory.json",
                    json.dumps([m.model_dump(mode="json") for m in mems], indent=2, default=str),
                )

        if "settings" in components and s.settings_json_path.exists():
            zf.write(s.settings_json_path, arcname="settings/settings.json")

        if "logs" in components and s.logs_path.exists():
            _zip_dir(zf, s.logs_path, "logs")

        if "cache" in components and s.cache_path.exists():
            _zip_dir(zf, s.cache_path, "cache")

        if "originals" in components:
            with session_scope() as session:
                docs = session.exec(select(Document)).all()
                for d in docs:
                    try:
                        p = Path(d.path)
                        if p.exists():
                            zf.write(p, arcname=f"originals/{d.id}_{safe_filename(p.name)}")
                    except Exception as e:
                        logger.warning("skipped original {}: {}", d.path, e)

        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    if encrypt_password and buf is not None:
        salt = os.urandom(16)
        f = Fernet(_derive_key(encrypt_password, salt))
        encrypted = f.encrypt(buf.getvalue())
        with output_path.open("wb") as out:
            out.write(b"LDIENC1")  # magic header
            out.write(salt)
            out.write(encrypted)

    size = output_path.stat().st_size

    # Record backup row
    with session_scope() as session:
        session.add(
            Backup(
                filename=output_path.name,
                path=str(output_path),
                size_bytes=size,
                components=sorted(components),
                app_version=__version__,
                encrypted=bool(encrypt_password),
            )
        )

    return BackupResult(
        path=output_path,
        size_bytes=size,
        components=sorted(components),
        encrypted=bool(encrypt_password),
    )


def list_backups() -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.exec(select(Backup).order_by(Backup.created_at.desc())).all()  # type: ignore
        return [r.model_dump(mode="json") for r in rows]


def _decrypt_if_needed(path: Path, password: str | None) -> bytes:
    data = path.read_bytes()
    if not data.startswith(b"LDIENC1"):
        return data
    if not password:
        raise ValueError("backup is encrypted; password required")
    salt = data[7:23]
    payload = data[23:]
    f = Fernet(_derive_key(password, salt))
    return f.decrypt(payload)


def restore_backup(
    archive_path: str | Path,
    *,
    components: Iterable[str] | None = None,
    password: str | None = None,
    make_safety_copy: bool = True,
) -> dict[str, Any]:
    s = get_settings()
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise FileNotFoundError(archive_path)

    if make_safety_copy:
        try:
            safety = create_backup(
                {"db", "vector", "chats", "memory", "settings"},
                output_path=s.backups_path / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip",
            )
            logger.info("safety backup at {}", safety.path)
        except Exception as e:
            logger.warning("safety backup failed: {}", e)

    raw = _decrypt_if_needed(archive_path, password)
    bio = io.BytesIO(raw)

    restored: list[str] = []
    errors: list[str] = []
    selected = set(components) if components else None

    with zipfile.ZipFile(bio, "r") as zf:
        manifest_raw = zf.read("manifest.json") if "manifest.json" in zf.namelist() else b"{}"
        manifest = json.loads(manifest_raw or b"{}")
        present = set(manifest.get("components", []))

        # DB
        if (selected is None or "db" in selected) and "db" in present:
            try:
                with zf.open("db/localdoc.db") as src, s.db_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored.append("db")
            except Exception as e:
                errors.append(f"db: {e}")

        # Vector
        if (selected is None or "vector" in selected) and "vector" in present:
            try:
                shutil.rmtree(s.chroma_path, ignore_errors=True)
                s.chroma_path.mkdir(parents=True, exist_ok=True)
                for name in zf.namelist():
                    if name.startswith("vector/"):
                        rel = name[len("vector/") :]
                        if not rel:
                            continue
                        target = s.chroma_path / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(name) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                restored.append("vector")
            except Exception as e:
                errors.append(f"vector: {e}")

        # Settings
        if (selected is None or "settings" in selected) and "settings/settings.json" in zf.namelist():
            try:
                with zf.open("settings/settings.json") as src, s.settings_json_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored.append("settings")
            except Exception as e:
                errors.append(f"settings: {e}")

    return {"restored": restored, "errors": errors, "manifest": manifest}
