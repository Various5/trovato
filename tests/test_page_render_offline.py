"""Offline-robust page serving: when the original PDF can't be read, serve any
cached render of the page so the document stays viewable (not blank)."""

from __future__ import annotations

from app.api.routes.documents import _any_cached_render, _fallback_render


def test_prefers_widest_bucket(tmp_path) -> None:
    (tmp_path / "page_0001_w768.png").write_bytes(b"x")
    (tmp_path / "page_0001_w1280.png").write_bytes(b"x")
    (tmp_path / "page_0001_w512.png").write_bytes(b"x")
    got = _any_cached_render(tmp_path, 1)
    assert got is not None and got.name == "page_0001_w1280.png"


def test_legacy_unbucketed_fallback(tmp_path) -> None:
    (tmp_path / "page_0002.png").write_bytes(b"x")
    got = _any_cached_render(tmp_path, 2)
    assert got is not None and got.name == "page_0002.png"


def test_none_when_no_render(tmp_path) -> None:
    assert _any_cached_render(tmp_path, 3) is None
    assert _any_cached_render(tmp_path / "missing", 1) is None


def test_ignores_empty_truncated_file(tmp_path) -> None:
    (tmp_path / "page_0001_w768.png").write_bytes(b"")  # mid-write kill
    assert _any_cached_render(tmp_path, 1) is None


def test_does_not_match_other_pages(tmp_path) -> None:
    (tmp_path / "page_0002_w768.png").write_bytes(b"x")
    assert _any_cached_render(tmp_path, 1) is None


def test_fallback_prefers_scan_pointer(tmp_path) -> None:
    rp = tmp_path / "scan.png"
    rp.write_bytes(b"x")
    (tmp_path / "page_0001_w768.png").write_bytes(b"x")

    class _Page:
        rendered_image_path = str(rp)

    assert _fallback_render(tmp_path, 1, _Page()) == rp


def test_fallback_to_bucket_when_no_pointer(tmp_path) -> None:
    (tmp_path / "page_0001_w768.png").write_bytes(b"x")
    got = _fallback_render(tmp_path, 1, None)
    assert got is not None and got.name == "page_0001_w768.png"
