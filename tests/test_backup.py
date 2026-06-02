"""Backup / restore: portability across machines (transfer the index, skip re-scan)."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

from sqlmodel import select

from app.backup import create_backup, restore_backup
from app.database import init_db, session_scope
from app.models import (
    Chat,
    ChatMessage,
    Document,
    DocumentChunk,
    DocumentSource,
    SourceType,
    User,
    UserMemory,
    UserRole,
    Visibility,
)


def test_create_and_restore_settings_backup(tmp_path: Path) -> None:
    init_db()
    out = tmp_path / "b.zip"
    res = create_backup(["settings", "chats", "memory"], output_path=out)
    assert out.exists()
    assert res.size_bytes > 0
    info = restore_backup(out, components=["settings"], make_safety_copy=False)
    assert "errors" in info


def _seed_document(path_str: str, content_hash: str) -> int:
    """Insert a source + document + one chunk; return the document id."""
    with session_scope() as s:
        src = DocumentSource(
            name="backup-src",
            type=SourceType.local,
            path="/srcroot",
            owner_id=None,
            visibility=Visibility.private,
        )
        s.add(src)
        s.flush()
        doc = Document(
            source_id=src.id,
            path=path_str,
            filename=Path(path_str.replace("\\", "/")).name,
            extension="pdf",
            content_hash=content_hash,
        )
        s.add(doc)
        s.flush()
        s.add(DocumentChunk(document_id=doc.id, page_from=1, page_to=1, text="hello world", token_count=2))
        return doc.id  # type: ignore[return-value]


def test_db_backup_captures_recent_writes(tmp_path: Path) -> None:
    """The WAL checkpoint means a just-written row is in the archived db file.

    Pre-fix the backup copied localdoc.db but not its -wal sidecar, so a row
    still in the WAL could be silently missing from the backup.
    """
    init_db()
    uniq = "wal_probe_hash_0001"
    _seed_document("/srcroot/wal_probe.pdf", uniq)

    out = tmp_path / "db.zip"
    create_backup(["db"], output_path=out)

    # Read the archived DB directly (independent of the live engine).
    with zipfile.ZipFile(out) as zf:
        data = zf.read("db/localdoc.db")
    extracted = tmp_path / "extracted.db"
    extracted.write_bytes(data)
    con = sqlite3.connect(str(extracted))
    try:
        (count,) = con.execute(
            "SELECT COUNT(*) FROM documents WHERE content_hash = ?", (uniq,)
        ).fetchone()
    finally:
        con.close()
    assert count == 1, "recent write missing from backup — WAL not checkpointed"


def test_db_restore_roundtrip(tmp_path: Path) -> None:
    """Back up the index, drop a document, restore db, and see it return."""
    init_db()
    uniq = "roundtrip_hash_0002"
    doc_id = _seed_document("/srcroot/roundtrip.pdf", uniq)

    out = tmp_path / "rt.zip"
    create_backup(["db"], output_path=out)

    with session_scope() as s:
        for ch in s.exec(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)).all():
            s.delete(ch)  # FK: chunks reference the document
        d = s.get(Document, doc_id)
        if d:
            s.delete(d)
    with session_scope() as s:
        assert s.exec(select(Document).where(Document.content_hash == uniq)).first() is None

    info = restore_backup(out, components=["db"], make_safety_copy=False)
    assert "db" in info["restored"], info

    with session_scope() as s:
        restored = s.exec(select(Document).where(Document.content_hash == uniq)).first()
        assert restored is not None
        chunks = s.exec(select(DocumentChunk).where(DocumentChunk.document_id == restored.id)).all()
        assert len(chunks) == 1


def test_restore_path_remap(tmp_path: Path) -> None:
    """path_remap rewrites stored document paths for a different machine layout."""
    init_db()
    uniq = "remap_hash_0003"
    _seed_document(r"C:\oldroot\reports\a.pdf", uniq)

    out = tmp_path / "remap.zip"
    create_backup(["db"], output_path=out)

    info = restore_backup(
        out,
        components=["db"],
        make_safety_copy=False,
        path_remap=(r"C:\oldroot", r"D:\docs"),
    )
    assert "db" in info["restored"], info
    assert info.get("path_remapped", 0) >= 1

    with session_scope() as s:
        d = s.exec(select(Document).where(Document.content_hash == uniq)).first()
        assert d is not None
        assert d.path == r"D:\docs\reports\a.pdf"


def test_chats_memory_selective_restore(tmp_path: Path) -> None:
    """Restoring chats/memory WITHOUT db merges them back from the JSON export."""
    init_db()
    with session_scope() as s:
        u = User(username="backup_user", password_hash="x", role=UserRole.user)
        s.add(u)
        s.flush()
        uid = u.id
        chat = Chat(user_id=uid, title="Backup Chat 42")
        s.add(chat)
        s.flush()
        cid = chat.id
        s.add(ChatMessage(chat_id=cid, role="user", content="remember this"))
        s.add(UserMemory(user_id=uid, key="fav_color", value="teal"))

    out = tmp_path / "cm.zip"
    create_backup(["chats", "memory"], output_path=out)

    # Drop them, then selectively restore (no db component → JSON merge path).
    with session_scope() as s:
        for m in s.exec(select(ChatMessage).where(ChatMessage.chat_id == cid)).all():
            s.delete(m)
        c = s.get(Chat, cid)
        if c:
            s.delete(c)
        for mem in s.exec(select(UserMemory).where(UserMemory.user_id == uid)).all():
            s.delete(mem)

    info = restore_backup(out, components=["chats", "memory"], make_safety_copy=False)
    assert "chats" in info["restored"], info
    assert "memory" in info["restored"], info

    with session_scope() as s:
        assert s.get(Chat, cid) is not None
        assert s.exec(select(ChatMessage).where(ChatMessage.chat_id == cid)).first() is not None
        mem = s.exec(select(UserMemory).where(UserMemory.user_id == uid)).first()
        assert mem is not None and mem.value == "teal"


def test_chats_memory_restore_skips_orphans(tmp_path: Path) -> None:
    """Restoring chats/memory whose user is absent skips those rows, not aborts.

    Reproduces the cross-machine case (renumbered/missing users) — must not blow
    up with a FOREIGN KEY error and lose everything.
    """
    init_db()
    with session_scope() as s:
        u = User(username="orphan_user", password_hash="x", role=UserRole.user)
        s.add(u)
        s.flush()
        uid = u.id
        chat = Chat(user_id=uid, title="Orphan Chat")
        s.add(chat)
        s.flush()
        cid = chat.id
        s.add(UserMemory(user_id=uid, key="k", value="v"))

    out = tmp_path / "orphan.zip"
    create_backup(["chats", "memory"], output_path=out)

    # Remove the chat/memory AND the user they depend on.
    with session_scope() as s:
        c = s.get(Chat, cid)
        if c:
            s.delete(c)
        for mem in s.exec(select(UserMemory).where(UserMemory.user_id == uid)).all():
            s.delete(mem)
    with session_scope() as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)

    # Must not raise; rows are skipped because the FK target user is gone.
    info = restore_backup(out, components=["chats", "memory"], make_safety_copy=False)
    assert "chats" in info["restored"], info
    assert "memory" in info["restored"], info
    assert any("skipped" in e for e in info["errors"]), info
    with session_scope() as s:
        assert s.get(Chat, cid) is None  # orphan chat was skipped, not inserted
