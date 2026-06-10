"""The license gate must be enforced server-side on /api/*, not only in the UI.

A UI-only redirect is bypassable: a logged-in-but-unlicensed user could pull the
library straight over HTTP. These tests pin the middleware in app.main that 403s
data routes while locked and lets auth/health through.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Isolated data dir so create_app()'s ensure_dirs/secret_key don't touch the
    # real one. Built once per module (NiceGUI page registration is global).
    data_dir = tmp_path_factory.mktemp("ldi-gate")
    prev = os.environ.get("LDI_DATA_DIR")
    os.environ["LDI_DATA_DIR"] = str(data_dir)
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    # No `with` → don't run the lifespan (warm-up/heal); we only exercise routing.
    c = TestClient(create_app())
    yield c
    if prev is None:
        os.environ.pop("LDI_DATA_DIR", None)
    else:
        os.environ["LDI_DATA_DIR"] = prev
    get_settings.cache_clear()


def test_api_blocked_when_unlicensed(client, monkeypatch):
    from app.services import licensing

    monkeypatch.setattr(licensing, "is_activated", lambda: False)
    # Data/content routes are refused outright.
    assert client.get("/api/documents").status_code == 403
    assert client.post("/api/search", json={"query": "x"}).status_code == 403
    assert client.get("/api/documents/1/file").status_code == 403
    # Liveness/auth stay open so login + the public liveness probe work while locked.
    assert client.get("/api/health/ping").status_code == 200


def test_api_open_when_licensed(client, monkeypatch):
    from app.services import licensing

    monkeypatch.setattr(licensing, "is_activated", lambda: True)
    # The license middleware passes the request through; the route's own auth
    # then applies (401 for no session) — crucially NOT 403 from the gate.
    assert client.get("/api/documents").status_code != 403
