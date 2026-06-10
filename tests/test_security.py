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


def test_about_and_health_require_auth(client):
    assert client.get("/api/about").status_code == 401
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health/ping").status_code == 200  # public liveness


def test_privileged_routers_require_auth(client):
    # No session → login_required (then require_admin) → 401, never 200.
    for path in ("/api/settings", "/api/sources", "/api/users", "/api/diagnostics/audit"):
        assert client.get(path).status_code in (401, 403, 404), path
        assert client.get(path).status_code != 200, path
