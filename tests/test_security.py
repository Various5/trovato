"""Regression tests for the security hardening: CSRF origin-check, security
headers, auth-gating of about/health, and admin-gating of privileged routers."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("ldi-sec")
    prev = os.environ.get("LDI_DATA_DIR")
    os.environ["LDI_DATA_DIR"] = str(data_dir)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    c = TestClient(create_app())
    yield c
    if prev is None:
        os.environ.pop("LDI_DATA_DIR", None)
    else:
        os.environ["LDI_DATA_DIR"] = prev
    get_settings.cache_clear()


def test_csrf_blocks_cross_origin_state_change(client):
    # A browser attaches Origin on cross-site requests; a mismatch on an unsafe
    # /api method is a CSRF attempt and must be rejected before auth runs.
    r = client.post(
        "/api/auth/login",
        json={"username": "x", "password": "y"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403


def test_same_origin_or_no_origin_allowed(client):
    # No Origin header (non-browser API client) is allowed through to the route.
    r = client.post("/api/auth/login", json={"username": "x", "password": "y"})
    assert r.status_code != 403  # 401 invalid creds — not CSRF-blocked


def test_security_headers_present(client):
    r = client.get("/api/health/ping")
    assert r.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "no-referrer"
    # CSP ships in Report-Only mode first (observe, don't enforce). It must
    # permit the NiceGUI ws/wss and not enforce (no plain Content-Security-Policy
    # header yet, so it can't break the viewer/WebSocket).
    csp = r.headers.get("Content-Security-Policy-Report-Only")
    assert csp and "connect-src 'self' ws: wss:" in csp
    assert r.headers.get("Content-Security-Policy") is None


def test_about_and_health_require_auth(client):
    assert client.get("/api/about").status_code == 401
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health/ping").status_code == 200  # public liveness


def test_privileged_routers_require_auth(client):
    # No session → login_required (then require_admin) → 401, never 200.
    for path in ("/api/settings", "/api/sources", "/api/users", "/api/diagnostics/audit"):
        assert client.get(path).status_code in (401, 403, 404), path
        assert client.get(path).status_code != 200, path


def test_session_fingerprint_changes_with_password():
    # The fingerprint drives password-change session revocation: a new password
    # → new hash → new fingerprint → old sessions stop validating.
    from app.auth.security import hash_password, session_fingerprint
    from app.models import User

    u = User(username="x", password_hash=hash_password("pw1"), recovery_key_hash="")
    fp1 = session_fingerprint(u)
    u.password_hash = hash_password("pw2")
    fp2 = session_fingerprint(u)
    assert fp1 and fp2 and fp1 != fp2


def test_lmstudio_rejects_link_local_metadata():
    from fastapi import HTTPException

    from app.api.routes.lmstudio import _reject_metadata_target

    with pytest.raises(HTTPException):
        _reject_metadata_target("http://169.254.169.254/v1")  # cloud metadata
    # Legitimate LM Studio targets (loopback / LAN / hostnames) pass.
    _reject_metadata_target("http://localhost:1234/v1")
    _reject_metadata_target("http://192.168.1.5:1234/v1")
    _reject_metadata_target(None)
