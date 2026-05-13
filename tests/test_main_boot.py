"""Defensive boot-helpers in app.main — make sure they don't regress."""

from __future__ import annotations

import os
import sys

from app.main import _crash_dump_dir, _ensure_console_streams, _write_crash_dump


def test_crash_dump_dir_is_writable() -> None:
    d = _crash_dump_dir()
    assert d.exists() and d.is_dir()
    sample = d / ".sentinel-test"
    sample.write_text("hello", encoding="utf-8")
    assert sample.read_text(encoding="utf-8") == "hello"
    sample.unlink()


def test_ensure_console_streams_replaces_none() -> None:
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]
        _ensure_console_streams()
        assert sys.stdout is not None
        assert sys.stderr is not None
        # Both should be writable (no AttributeError)
        sys.stdout.write("test")
        sys.stderr.write("test")
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err


def test_write_crash_dump_creates_file() -> None:
    try:
        raise RuntimeError("intentional test crash")
    except RuntimeError as e:
        path = _write_crash_dump(e, stage="unit-test")
    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8", errors="replace")
    assert "intentional test crash" in content
    assert "stage:" in content
    assert "python:" in content
    path.unlink()


def test_write_crash_dump_swallows_errors(monkeypatch) -> None:
    """Even if the dump dir is unwritable, the function must not raise."""
    monkeypatch.setattr("app.main._crash_dump_dir", lambda: __import__("pathlib").Path("/?/?/?invalid"))
    try:
        raise ValueError("boom")
    except ValueError as e:
        # Must not raise; may return None.
        path = _write_crash_dump(e, stage="oops")
    assert path is None or path.exists()


def test_health_ping_is_unauthenticated() -> None:
    """Smoke tests rely on /api/health/ping being public."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/api/health/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "version" in r.json()


_ = os  # silence unused
