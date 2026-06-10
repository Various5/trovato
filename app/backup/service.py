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
from app.database import session_scope, write_session
from app.models import Backup, Chat, ChatMessage, Document, DocumentChunk, DocumentPage, UserMemory
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


def _consistent_db_snapshot(db_path: Path, dest_dir: Path) -> Path:
    """Return a consistent, fully self-contained copy of the SQLite DB.

    Uses ``VACUUM INTO``, which writes a checkpointed standalone database (no
    ``-wal`` sidecar) reflecting all *committed* data — even while the app keeps
    reading and writing. This is immune to the failure mode of a raw file copy:
    in WAL mode a concurrent reader can block a checkpoint, leaving recent
    commits only in the (uncopied) ``-wal`` and yielding a stale or unreadable
    backup. The caller must delete the returned file's parent directory.
    """
    import sqlite3
    import tempfile

    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(dest_dir))) / "localdoc.db"
    literal = str(tmp).replace("'", "''")  # SQL string literal; double any quote
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(f"VACUUM INTO '{literal}'")
    finally:
        con.close()
    return tmp


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

        # DB — a VACUUM INTO snapshot (consistent + self-contained, all
        # committed data, no -wal needed) rather than a raw file copy that could
        # miss recently-committed rows still sitting in the WAL.
        if "db" in components and s.db_path.exists():
            snapshot = _consistent_db_snapshot(s.db_path, s.cache_path)
            try:
                zf.write(snapshot, arcname="db/localdoc.db")
            finally:
                shutil.rmtree(snapshot.parent, ignore_errors=True)

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


def _merge_rows(session: Any, model: Any, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """``merge()`` each row in its own SAVEPOINT; return ``(ok, skipped)``.

    A row that violates a constraint — e.g. a chat or memory whose ``user_id``
    doesn't exist on this machine (foreign_keys are ON) — is rolled back to its
    savepoint and skipped, instead of aborting the entire batch and leaving
    nothing restored.
    """
    ok = skipped = 0
    for row in rows:
        try:
            with session.begin_nested():
                session.merge(model.model_validate(row))
            ok += 1
        except Exception as e:
            skipped += 1
            logger.debug("restore skipped a {} row: {}", getattr(model, "__name__", model), e)
    return ok, skipped


def restore_backup(
    archive_path: str | Path,
    *,
    components: Iterable[str] | None = None,
    password: str | None = None,
    make_safety_copy: bool = True,
    path_remap: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Restore selected components from a backup archive.

    ``path_remap=(old_prefix, new_prefix)`` rewrites every ``Document.path`` that
    starts with ``old_prefix`` to ``new_prefix`` after a DB restore — use it when
    moving an index to a machine that stores the original files in a different
    folder, so opening/previewing the PDFs works there. Search/browse never need
    the files and work regardless.
    """
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

        # DB — extract to a temp file then atomically swap it in. The atomic
        # os.replace() means a concurrent reader never sees a half-written/0-byte
        # database, and we drop the old DB's -wal/-shm sidecars so SQLite can't
        # replay stale frames onto the freshly restored file.
        if (selected is None or "db" in selected) and "db" in present:
            from app.database.engine import reset_engine

            tmp_db = s.db_path.with_name(s.db_path.name + ".restore-tmp")
            try:
                with zf.open("db/localdoc.db") as src, tmp_db.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                # Release pooled handles (Windows blocks replace/unlink on open
                # files); the next get_engine() reconnects to the new DB — no
                # restart needed.
                reset_engine()
                for sfx in ("-wal", "-shm"):
                    sidecar = s.db_path.with_name(s.db_path.name + sfx)
                    try:
                        sidecar.unlink()
                    except FileNotFoundError:
                        pass
                os.replace(tmp_db, s.db_path)  # atomic
                restored.append("db")
            except Exception as e:
                errors.append(f"db: {e}")
                try:
                    tmp_db.unlink()
                except FileNotFoundError:
                    pass

        # Vector
        if (selected is None or "vector" in selected) and "vector" in present:
            try:
                shutil.rmtree(s.chroma_path, ignore_errors=True)
                s.chroma_path.mkdir(parents=True, exist_ok=True)
                base = s.chroma_path.resolve()
                for name in zf.namelist():
                    if name.startswith("vector/"):
                        rel = name[len("vector/") :]
                        if not rel:
                            continue
                        target = (s.chroma_path / rel).resolve()
                        # Zip-Slip guard: a crafted entry like 'vector/../../foo'
                        # must never write outside chroma_path.
                        if not target.is_relative_to(base):
                            errors.append(f"vector: refused unsafe path {name!r}")
                            continue
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

        # Chats + memory (JSON). Skipped when the whole DB was just restored —
        # those tables already came in with the .db file; the JSON path is for
        # selectively merging chats/memory into an existing database.
        db_restored = "db" in restored
        if (
            not db_restored
            and (selected is None or "chats" in selected)
            and "chats/chats.json" in zf.namelist()
        ):
            try:
                payload = json.loads(zf.read("chats/chats.json") or b"{}")
                with write_session() as session:
                    _, c_skip = _merge_rows(session, Chat, payload.get("chats", []))
                    session.flush()  # chats must exist before their messages (FK)
                    _, m_skip = _merge_rows(session, ChatMessage, payload.get("messages", []))
                restored.append("chats")
                if c_skip or m_skip:
                    errors.append(
                        f"chats: skipped {c_skip} chat(s) / {m_skip} message(s) "
                        "with no matching user/chat on this machine"
                    )
            except Exception as e:
                errors.append(f"chats: {e}")

        if (
            not db_restored
            and (selected is None or "memory" in selected)
            and "memory/memory.json" in zf.namelist()
        ):
            try:
                rows = json.loads(zf.read("memory/memory.json") or b"[]")
                with write_session() as session:
                    _, skip = _merge_rows(session, UserMemory, rows)
                restored.append("memory")
                if skip:
                    errors.append(f"memory: skipped {skip} item(s) with no matching user on this machine")
            except Exception as e:
                errors.append(f"memory: {e}")

    out: dict[str, Any] = {"restored": restored, "errors": errors, "manifest": manifest}

    # Optional cross-machine path remap (after the DB is in place).
    if path_remap and "db" in restored:
        old, new = path_remap
        try:
            with write_session() as session:
                remapped = 0
                if old:
                    for d in session.exec(select(Document)).all():
                        if d.path.startswith(old):
                            d.path = new + d.path[len(old) :]
                            session.add(d)
                            remapped += 1
                # Cached page renders point at the source machine's cache dir;
                # clear the pointers so the viewer re-renders from the (remapped)
                # originals on this machine.
                session.connection().exec_driver_sql(
                    f"UPDATE {DocumentPage.__tablename__} SET rendered_image_path = NULL"
                )
            out["path_remapped"] = remapped
        except Exception as e:
            errors.append(f"path_remap: {e}")

    return out
