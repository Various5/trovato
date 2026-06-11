"""Width-bucketed page renders (?w=) and on-page match-rect highlights.

Uses a tiny PDF generated with fitz so the render path and the rect
normalization (including a rotated page) are exercised end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import documents
from app.api.routes.documents import PAGE_WIDTH_BUCKETS, snap_width_bucket
from app.auth.security import create_user, make_media_token
from app.database import init_db, session_scope

fitz = pytest.importorskip("fitz")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(documents.router, prefix="/api/documents")
    return TestClient(app, raise_server_exceptions=False)


def _make_pdf(path: Path) -> None:
    """3 pages: text on 1 + 3 (page 3 rotated 90°), nothing relevant on 2."""
    doc = fitz.open()
    p1 = doc.new_page(width=595, height=842)
    p1.insert_text((100, 100), "Hello pool world. The pool is here.")
    p2 = doc.new_page(width=595, height=842)
    p2.insert_text((100, 200), "Nothing relevant on this page.")
    p3 = doc.new_page(width=595, height=842)
    p3.insert_text((50, 300), "pool again")
    p3.set_rotation(90)
    doc.save(str(path))
    doc.close()


def _make_doc(pdf_path: Path, *, username: str) -> tuple[int, int]:
    """(doc_id, owner_id) for a private single-owner document over pdf_path."""
    from app.models import Document, DocumentSource, DocumentStatus, SourceType, UserRole, Visibility

    init_db()
    with session_scope() as session:
        owner = create_user(session, username=username, password="pw-123456", role=UserRole.user)
        session.flush()
        owner_id = owner.id
        src = DocumentSource(
            name=f"src-{username}",
            type=SourceType.local,
            path=str(pdf_path.parent),
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(src)
        session.flush()
        doc = Document(
            source_id=src.id,
            path=str(pdf_path),
            filename=pdf_path.name,
            content_hash=f"hash-{username}",
            status=DocumentStatus.indexed,
            page_count=3,
            owner_id=owner_id,
            visibility=Visibility.private,
        )
        session.add(doc)
        session.flush()
        return doc.id, owner_id


# ---------------------------------------------------------------------------
# Width buckets
# ---------------------------------------------------------------------------


def test_snap_width_bucket_clamps() -> None:
    assert snap_width_bucket(1) == PAGE_WIDTH_BUCKETS[0]
    assert snap_width_bucket(PAGE_WIDTH_BUCKETS[0]) == PAGE_WIDTH_BUCKETS[0]
    assert snap_width_bucket(PAGE_WIDTH_BUCKETS[0] + 1) == PAGE_WIDTH_BUCKETS[1]
    assert snap_width_bucket(10**9) == PAGE_WIDTH_BUCKETS[-1]


def test_page_image_width_bucket_renders_and_caches(tmp_path) -> None:
    pdf = tmp_path / "bucket.pdf"
    _make_pdf(pdf)
    doc_id, owner_id = _make_doc(pdf, username="bucket-owner")
    client = _client()
    tok = make_media_token(owner_id)

    # An absurd ?w= is clamped to the largest bucket, not rendered verbatim.
    r = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": tok, "w": 999999})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # no-cache (NOT max-age): the browser stores the bytes but every use
    # revalidates — so logout/ACL changes take effect on the next request.
    assert "no-cache" in r.headers.get("cache-control", "")
    etag = r.headers.get("etag")
    assert etag

    # Conditional GET revalidation: 304, empty body, auth still enforced.
    r304 = client.get(
        f"/api/documents/{doc_id}/page/1/image",
        params={"t": tok, "w": 999999},
        headers={"If-None-Match": etag},
    )
    assert r304.status_code == 304
    r401 = client.get(
        f"/api/documents/{doc_id}/page/1/image",
        params={"w": 999999},
        headers={"If-None-Match": etag},
    )
    assert r401.status_code == 401

    from app.config import get_settings

    cache_dir = get_settings().cache_path / "pages" / str(doc_id)
    bucket_file = cache_dir / f"page_0001_w{PAGE_WIDTH_BUCKETS[-1]}.png"
    assert bucket_file.exists()
    # Rendered pixel width matches the bucket.
    pix = fitz.Pixmap(str(bucket_file))
    assert abs(pix.width - PAGE_WIDTH_BUCKETS[-1]) <= 2

    # Small bucket gets its own file; the legacy (no-w) name is untouched.
    r2 = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": tok, "w": 100})
    assert r2.status_code == 200
    assert (cache_dir / f"page_0001_w{PAGE_WIDTH_BUCKETS[0]}.png").exists()
    assert not (cache_dir / "page_0001.png").exists()

    # Legacy path (no w) still renders the 2x file under the legacy name.
    r3 = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": tok})
    assert r3.status_code == 200
    assert (cache_dir / "page_0001.png").exists()

    # Out-of-range page still 404s on the bucketed path.
    r4 = client.get(f"/api/documents/{doc_id}/page/99/image", params={"t": tok, "w": 800})
    assert r4.status_code == 404


def test_page_image_served_from_cache_when_original_offline(tmp_path) -> None:
    """Already-rendered pages must keep working when the source file is gone
    (unplugged drive / unreachable share) — cached bucket first, then the
    scan's rendered_image_path, then 410 only if nothing exists."""
    pdf = tmp_path / "offline.pdf"
    _make_pdf(pdf)
    doc_id, owner_id = _make_doc(pdf, username="offline-owner")
    client = _client()
    tok = make_media_token(owner_id)

    # Warm the bucket cache for page 1, then take the original away.
    r = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": tok, "w": 800})
    assert r.status_code == 200
    pdf.unlink()

    r2 = client.get(f"/api/documents/{doc_id}/page/1/image", params={"t": tok, "w": 800})
    assert r2.status_code == 200  # served from the warmed bucket cache

    # Page 2 has no bucket file but a scan render exists → fallback, not 410.
    from app.models import DocumentPage as _DP

    render = tmp_path / "page2-scan-render.png"
    d = fitz.open()
    pg = d.new_page(width=595, height=842)
    render.write_bytes(pg.get_pixmap().tobytes("png"))
    d.close()
    with session_scope() as session:
        session.add(
            _DP(document_id=doc_id, page_number=2, rendered_image_path=str(render), width=595, height=842)
        )
    r3 = client.get(f"/api/documents/{doc_id}/page/2/image", params={"t": tok, "w": 800})
    assert r3.status_code == 200

    # Page 3: no render anywhere → 410.
    r4 = client.get(f"/api/documents/{doc_id}/page/3/image", params={"t": tok, "w": 800})
    assert r4.status_code == 410


def test_purge_page_render_cache_removes_stale_renders(tmp_path) -> None:
    from app.config import get_settings
    from app.services.indexer import purge_page_render_cache

    init_db()
    cache_dir = get_settings().cache_path / "pages" / "424242"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "page_0001.png").write_bytes(b"x")
    (cache_dir / "page_0001_w1024.png").write_bytes(b"x")
    (cache_dir / "page_0002_w3072.png").write_bytes(b"x")
    purge_page_render_cache(424242)
    assert not list(cache_dir.glob("page_*.png"))
    # Nonexistent dir is a no-op, not an error.
    purge_page_render_cache(999999999)


# ---------------------------------------------------------------------------
# Match rects — service
# ---------------------------------------------------------------------------


def test_match_rects_service_normalizes_and_handles_rotation(tmp_path) -> None:
    from app.services.page_matches import match_rects_for_pages

    pdf = tmp_path / "rects.pdf"
    _make_pdf(pdf)
    rects = match_rects_for_pages(str(pdf), [1, 2, 3, 99], ["pool"])
    assert set(rects) == {1, 3}
    assert len(rects[1]) == 2  # "pool" twice on page 1
    for page_rects in rects.values():
        for x, y, w, h in page_rects:
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0, (x, y)
            assert 0.0 < w <= 1.0 and 0.0 < h <= 1.0, (w, h)
            assert x + w <= 1.001 and y + h <= 1.001


def test_match_rects_service_empty_inputs(tmp_path) -> None:
    from app.services.page_matches import match_rects_for_pages

    pdf = tmp_path / "empty.pdf"
    _make_pdf(pdf)
    assert match_rects_for_pages(str(pdf), [], ["pool"]) == {}
    assert match_rects_for_pages(str(pdf), [1], []) == {}
    assert match_rects_for_pages(str(tmp_path / "missing.pdf"), [1], ["pool"]) == {}


# ---------------------------------------------------------------------------
# Match rects — endpoint
# ---------------------------------------------------------------------------


def test_matches_endpoint_returns_rects_and_enforces_acl(tmp_path) -> None:
    from app.models import UserRole

    pdf = tmp_path / "ep.pdf"
    _make_pdf(pdf)
    doc_id, owner_id = _make_doc(pdf, username="match-owner")
    with session_scope() as session:
        other = create_user(session, username="match-other", password="pw-123456", role=UserRole.user)
        session.flush()
        other_id = other.id

    client = _client()
    r = client.get(
        f"/api/documents/{doc_id}/matches",
        params={"t": make_media_token(owner_id), "q": "where is the pool", "pages": "1,2,3"},
    )
    assert r.status_code == 200
    rects = r.json()["rects"]
    assert "1" in rects and "3" in rects and "2" not in rects
    assert all(len(rect) == 4 for rect in rects["1"])

    # Stopword-only query → no terms → empty result, not an error.
    r_empty = client.get(
        f"/api/documents/{doc_id}/matches",
        params={"t": make_media_token(owner_id), "q": "the of and", "pages": "1"},
    )
    assert r_empty.status_code == 200
    assert r_empty.json()["rects"] == {}

    # Garbage page lists are skipped gracefully — including Unicode 'digits'
    # like '²' that pass isdigit() but crash int(), and absurdly long parts.
    r_garbage = client.get(
        f"/api/documents/{doc_id}/matches",
        params={"t": make_media_token(owner_id), "q": "pool", "pages": "²,x,-3,9999999999999,1"},
    )
    assert r_garbage.status_code == 200
    assert "1" in r_garbage.json()["rects"]

    # Non-owner is forbidden (rect positions are document content).
    r_other = client.get(
        f"/api/documents/{doc_id}/matches",
        params={"t": make_media_token(other_id), "q": "pool", "pages": "1"},
    )
    assert r_other.status_code == 403

    # No token → unauthorized.
    r_anon = client.get(f"/api/documents/{doc_id}/matches", params={"q": "pool", "pages": "1"})
    assert r_anon.status_code == 401


def test_media_token_is_reused_within_window() -> None:
    init_db()
    with session_scope() as session:
        user = create_user(session, username="token-reuse", password="pw-123456")
        session.flush()
        uid = user.id
    assert make_media_token(uid) == make_media_token(uid)
