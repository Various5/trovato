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
