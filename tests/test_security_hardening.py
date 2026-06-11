"""v0.7.3 security hardening — path containment, admin gating, ACL, media-token
revocation, DoS limits, constant-time login.

Builds a self-contained app (SessionMiddleware + body-size cap + the routers
under test) over the session-isolated data dir, so authenticated/admin behavior
is exercised end to end without touching NiceGUI global page registration.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse


@pytest.fixture(scope="module")
def app():
    """A self-contained app: SessionMiddleware + the body-size cap + the routers
    under test, mounted WITHOUT the router-level admin dependency so the
    per-endpoint ``require_admin`` we added is what gets exercised. Uses the
    session-wide isolated data dir (conftest) so the DB engine and settings
    agree — no cross-module data-dir override."""
    from app.api.routes import auth, backup, chat, documents, scan, sources
    from app.config import get_settings

    a = FastAPI()
    a.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key, same_site="lax")

    _MAX = 4 * 1024 * 1024

    @a.middleware("http")
    async def _body_cap(request, call_next):  # type: ignore[no-untyped-def]
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            cl = request.headers.get("content-length")
            if cl is not None and cl.isdigit() and int(cl) > _MAX:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
        return await call_next(request)

    a.include_router(auth.router, prefix="/api/auth")
    a.include_router(sources.router, prefix="/api/sources")
    a.include_router(scan.router, prefix="/api/scan")
    a.include_router(backup.router, prefix="/api/backup")
    a.include_router(documents.router, prefix="/api/documents")
    a.include_router(chat.router, prefix="/api/chats")
    return a


@pytest.fixture(scope="module")
def users():
    """Create one admin + one normal user; return their ids."""
    from sqlmodel import select

    from app.auth.security import create_user
    from app.database import init_db, session_scope
    from app.models import User, UserRole

    init_db()
    with session_scope() as session:
        if session.exec(select(User).where(User.username == "admin")).first() is None:
            create_user(session, username="admin", password="pw-admin-123456", role=UserRole.admin)
        if session.exec(select(User).where(User.username == "bob")).first() is None:
            create_user(session, username="bob", password="pw-bob-123456", role=UserRole.user)
        return {u.username: u.id for u in session.exec(select(User)).all()}


def data_dir_path():
    from app.config import get_settings

    return get_settings().data_path


def _login(app, username, password) -> TestClient:
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


# ---------------------------------------------------------------------------
# A2 — admin gating + source-path confinement
# ---------------------------------------------------------------------------


def test_source_creation_requires_admin(app, users):
    bob = _login(app, "bob", "pw-bob-123456")
    r = bob.post("/api/sources", json={"name": "x", "path": "/tmp/docs"})
    assert r.status_code == 403


def test_source_path_root_and_datadir_rejected(app, users):
    admin = _login(app, "admin", "pw-admin-123456")
    # A filesystem root is too broad.
    root = "C:\\" if os.name == "nt" else "/"
    r_root = admin.post("/api/sources", json={"name": "r", "path": root})
    assert r_root.status_code == 400
    # The app's own data dir holds secret.key + the DB.
    r_data = admin.post("/api/sources", json={"name": "d", "path": str(data_dir_path())})
    assert r_data.status_code == 400


def test_source_creation_admin_ok(app, users, tmp_path):
    admin = _login(app, "admin", "pw-admin-123456")
    folder = tmp_path / "mydocs"
    folder.mkdir()
    r = admin.post("/api/sources", json={"name": "ok", "path": str(folder)})
    assert r.status_code == 200, r.text


def test_scan_and_backup_require_admin(app, users):
    bob = _login(app, "bob", "pw-bob-123456")
    assert bob.post("/api/scan/start", json={"source_id": 1}).status_code == 403
    assert bob.post("/api/backup", json={"components": ["db"]}).status_code == 403


# ---------------------------------------------------------------------------
# A1 — path containment on file serving
# ---------------------------------------------------------------------------


def _make_doc(env, *, path: str, source_path: str, owner: str, users) -> int:
    from app.database import session_scope
    from app.models import (
        Document,
        DocumentSource,
        DocumentStatus,
        SourceType,
        Visibility,
    )

    with session_scope() as session:
        src = DocumentSource(
            name=f"src-{owner}-{abs(hash(path)) % 9999}",
            type=SourceType.local,
            path=source_path,
            owner_id=users[owner],
            visibility=Visibility.private,
        )
        session.add(src)
        session.flush()
        doc = Document(
            source_id=src.id,
            path=path,
            filename="x.pdf",
            content_hash=f"h-{abs(hash(path)) % 99999}",
            status=DocumentStatus.indexed,
            page_count=1,
            owner_id=users[owner],
            visibility=Visibility.private,
        )
        session.add(doc)
        session.flush()
        return doc.id


def test_download_rejects_path_outside_source(app, users, tmp_path):
    # Document path lives OUTSIDE its source root → containment guard 403s.
    src_root = tmp_path / "srcroot"
    src_root.mkdir()
    outside = tmp_path / "secret.key"
    outside.write_text("SECRET")
    doc_id = _make_doc(env=None, path=str(outside), source_path=str(src_root), owner="admin", users=users)
    admin = _login(app, "admin", "pw-admin-123456")
    r = admin.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 403
    assert b"SECRET" not in r.content


def test_download_allows_path_inside_source(app, users, tmp_path):
    src_root = tmp_path / "srcroot2"
    src_root.mkdir()
    inside = src_root / "doc.pdf"
    inside.write_bytes(b"%PDF-1.4 fake")
    doc_id = _make_doc(env=None, path=str(inside), source_path=str(src_root), owner="admin", users=users)
    admin = _login(app, "admin", "pw-admin-123456")
    r = admin.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 200  # under source root → served


# ---------------------------------------------------------------------------
# B1 — ACL on compare / similar / summarize
# ---------------------------------------------------------------------------


def test_acl_blocks_cross_user_compare_similar_summarize(app, users, tmp_path):
    # A private doc owned by admin must be invisible to bob on every route.
    root = tmp_path / "adminroot"
    root.mkdir()
    p = root / "private.pdf"
    p.write_bytes(b"%PDF-1.4")
    doc_id = _make_doc(env=None, path=str(p), source_path=str(root), owner="admin", users=users)
    doc2 = _make_doc(env=None, path=str(p), source_path=str(root), owner="admin", users=users)
    bob = _login(app, "bob", "pw-bob-123456")
    assert bob.get(f"/api/documents/{doc_id}/similar").status_code == 404
    assert bob.get(f"/api/documents/compare/{doc_id}/{doc2}").status_code == 404
    assert bob.post(f"/api/chats/summarize/{doc_id}").status_code == 404


# ---------------------------------------------------------------------------
# B2 — media-token revocation
# ---------------------------------------------------------------------------


def test_media_token_with_stale_fingerprint_rejected(app, users, tmp_path):
    from app.auth.security import make_media_token

    root = tmp_path / "mroot"
    root.mkdir()
    p = root / "m.pdf"
    p.write_bytes(b"%PDF-1.4")
    doc_id = _make_doc(env=None, path=str(p), source_path=str(root), owner="admin", users=users)
    c = TestClient(app)  # no cookie → must use ?t=
    good = make_media_token(users["admin"])  # fp derived from DB
    stale = make_media_token(users["admin"], "fp-from-before-password-change")
    assert c.get(f"/api/documents/{doc_id}/file", params={"t": stale}).status_code == 401
    assert c.get(f"/api/documents/{doc_id}/file", params={"t": good}).status_code != 401


# ---------------------------------------------------------------------------
# B3 — DoS limits + constant-time login
# ---------------------------------------------------------------------------


def test_oversized_body_rejected(app, users):
    c = TestClient(app)
    big = b'{"username":"a","password":"' + b"x" * (5 * 1024 * 1024) + b'"}'
    r = c.post("/api/auth/login", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_login_same_response_for_missing_and_wrong_user(app, users):
    c = TestClient(app)
    r_missing = c.post("/api/auth/login", json={"username": "nope-xyz", "password": "whatever-123456"})
    r_wrong = c.post("/api/auth/login", json={"username": "admin", "password": "wrong-pw-123456"})
    assert r_missing.status_code == 401
    assert r_wrong.status_code == 401
    assert r_missing.json()["detail"] == r_wrong.json()["detail"]


def test_rate_limit_map_does_not_allocate_on_is_locked():
    from app.auth import rate_limit

    rate_limit._STATE.clear()
    locked, _ = rate_limit.is_locked("1.2.3.4", "ghost-user")
    assert locked is False
    # is_locked must NOT have inserted an entry (the unbounded-map DoS fix).
    assert ("1.2.3.4", "ghost-user") not in rate_limit._STATE
    # A real failure does create one.
    rate_limit.record_failure("1.2.3.4", "ghost-user")
    assert ("1.2.3.4", "ghost-user") in rate_limit._STATE
    rate_limit._STATE.clear()
