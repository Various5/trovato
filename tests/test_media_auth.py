"""Integration test for media_user: PDF / page-image endpoints accept either a
session cookie or a signed ``?t=`` token.

Drives the documents router through a TestClient with no session middleware, so
only the token path is exercised — exactly the new-tab / <img> scenario behind
the "open in browser → login required" report.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import documents
from app.auth.security import create_user, make_media_token
from app.database import init_db, session_scope


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(documents.router, prefix="/api/documents")
    return TestClient(app, raise_server_exceptions=False)


def test_file_without_token_is_unauthorized() -> None:
    init_db()
    r = _client().get("/api/documents/999999/file")
    assert r.status_code == 401


def test_file_with_bad_token_is_unauthorized() -> None:
    init_db()
    r = _client().get("/api/documents/999999/file", params={"t": "not-a-real-token"})
    assert r.status_code == 401


def test_file_with_valid_token_passes_auth() -> None:
    init_db()
    with session_scope() as session:
        user = create_user(session, username="media-token-user", password="pw-123456")
        session.flush()
        uid = user.id
    token = make_media_token(uid)
    # Document doesn't exist, so a *non-401* (404) proves the token cleared auth.
    r = _client().get("/api/documents/999999/file", params={"t": token})
    assert r.status_code != 401
    assert r.status_code == 404


def test_page_image_enforces_per_document_acl() -> None:
    """A page image is document content, so get_page_image must apply the same
    owner/shared/admin ACL as download_document — not just authenticate. A
    non-owner with a valid token of their own must NOT be able to read another
    user's private document by iterating IDs.
    """
    from app.models import (
        Document,
        DocumentSource,
        DocumentStatus,
        SourceType,
        UserRole,
        Visibility,
    )

    init_db()
    with session_scope() as session:
        owner = create_user(session, username="idor-owner", password="pw-123456", role=UserRole.user)
        other = create_user(session, username="idor-other", password="pw-123456", role=UserRole.user)
        session.flush()
        owner_id, other_id = owner.id, other.id
        src = DocumentSource(
            name="idor-src",
            type=SourceType.local,
            path="/tmp/idor",
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(src)
        session.flush()
        doc = Document(
            source_id=src.id,
            path="/tmp/idor/secret-does-not-exist.pdf",
            filename="secret.pdf",
            content_hash="idor-hash",
            status=DocumentStatus.indexed,
            page_count=1,
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(doc)
        session.flush()
        doc_id = doc.id

    client = _client()
    # Non-owner, non-admin → forbidden (must NOT leak the private page image).
    r_other = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": make_media_token(other_id)})
    assert r_other.status_code == 403

    # Owner clears the ACL (then 410 because the file isn't on disk) — anything
    # but 401/403 proves authorization passed.
    r_owner = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": make_media_token(owner_id)})
    assert r_owner.status_code not in (401, 403)


def test_extracted_image_route_serves_and_acls(tmp_path) -> None:
    """The embedded-image endpoint serves the cached file to the owner, applies
    the per-document ACL, and 404s an unknown image."""
    from app.models import (
        Document,
        DocumentImage,
        DocumentSource,
        DocumentStatus,
        SourceType,
        UserRole,
        Visibility,
    )

    init_db()
    imgfile = tmp_path / "logo.png"
    imgfile.write_bytes(b"\x89PNG\r\n\x1a\nFAKE-IMAGE-BYTES")
    with session_scope() as session:
        owner = create_user(session, username="img-owner", password="pw-123456", role=UserRole.user)
        other = create_user(session, username="img-other", password="pw-123456", role=UserRole.user)
        session.flush()
        owner_id, other_id = owner.id, other.id
        src = DocumentSource(
            name="img-src",
            type=SourceType.local,
            path="/tmp/img",
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(src)
        session.flush()
        doc = Document(
            source_id=src.id,
            path="/tmp/img/x.pdf",
            filename="x.pdf",
            content_hash="img-route-hash",
            status=DocumentStatus.indexed,
            page_count=1,
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(doc)
        session.flush()
        img = DocumentImage(
            document_id=doc.id, page_number=1, image_index=0, image_hash="ih1", cache_path=str(imgfile)
        )
        session.add(img)
        session.flush()
        doc_id, img_id = doc.id, img.id

    client = _client()
    r = client.get(f"/api/documents/{doc_id}/img/{img_id}", params={"t": make_media_token(owner_id)})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")

    r_other = client.get(f"/api/documents/{doc_id}/img/{img_id}", params={"t": make_media_token(other_id)})
    assert r_other.status_code == 403

    r_missing = client.get(f"/api/documents/{doc_id}/img/999999", params={"t": make_media_token(owner_id)})
    assert r_missing.status_code == 404
