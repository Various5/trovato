"""Document listing, details, and file streaming."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlmodel import Session, select

from app.auth.security import (
    current_user_id,
    login_required,
    session_fingerprint,
    verify_media_token,
)
from app.database import get_session, write_session
from app.models import Document, DocumentImage, DocumentPage, DocumentSource, User

router = APIRouter()


def _served_path_allowed(path: Path, doc: Document, session: Session) -> bool:
    """Containment guard for any file streamed to a client.

    Without this, ``download_document`` / ``get_*_image`` would stream
    ``Document.path`` / ``cache_path`` verbatim — and an admin can point a
    source at ``C:\\`` (or the app's own data dir) and catalog arbitrary host
    files, turning these routes into an arbitrary-file-read primitive. We only
    serve a file that lives under (a) the app CACHE dir (all app-generated page
    renders / extracted images) or (b) the configured root of the document's
    OWNING source (the legitimate location of the original). The cache dir ONLY,
    NOT the whole data dir — the data dir also holds ``secret.key`` and the DB.
    """
    from app.config import get_settings
    from app.utils.paths import is_under

    s = get_settings()
    if is_under(path, s.cache_path):
        return True
    src = session.get(DocumentSource, doc.source_id) if doc.source_id else None
    return bool(src and src.path and is_under(path, src.path))


def media_user(
    request: Request,
    t: str | None = None,
    session: Session = Depends(get_session),
) -> User:
    """Auth for media URLs: accept the session cookie OR a signed ``?t=`` token.

    PDF "open in browser" (new tab) and page-image previews (``<img>`` src) can't
    rely on the NiceGUI session being carried, so the UI appends a signed token.

    Both paths re-check the password fingerprint (``pwv``), so a password change
    revokes a stolen cookie AND any outstanding ``?t=`` media token — the token
    is no longer a 24h credential that outlives credential rotation.
    """
    uid = current_user_id(request)
    token_fp: str | None = None
    via_token = False
    if uid is None and t:
        payload = verify_media_token(t)
        if payload:
            uid = payload["uid"]
            token_fp = payload["fp"]
            via_token = True
    if uid is None:
        raise HTTPException(status_code=401, detail="login required")
    user = session.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="login required")
    expected_fp = session_fingerprint(user)
    if via_token:
        if token_fp != expected_fp:
            raise HTTPException(status_code=401, detail="token expired")
    elif request.session.get("pwv") != expected_fp:
        # Cookie path: enforce pwv revocation like get_current_user.
        raise HTTPException(status_code=401, detail="session expired")
    return user


@router.get("")
def list_documents(
    q: str | None = None,
    source_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    from app.auth.acl import filter_documents

    stmt = filter_documents(select(Document), user)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Document.filename.like(like), Document.path.like(like)))  # type: ignore
    if source_id is not None:
        stmt = stmt.where(Document.source_id == source_id)
    if status:
        stmt = stmt.where(Document.status == status)
    stmt = stmt.order_by(Document.id.desc()).offset(offset).limit(limit)  # type: ignore
    rows = session.exec(stmt).all()
    return [r.model_dump(mode="json") for r in rows]


@router.get("/{doc_id}")
def get_document(
    doc_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    from app.auth.acl import can_see_document

    d = session.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    if not can_see_document(user, d):
        raise HTTPException(status_code=403, detail="forbidden")
    pages = session.exec(
        select(DocumentPage).where(DocumentPage.document_id == doc_id).order_by(DocumentPage.page_number)
    ).all()
    images = session.exec(select(DocumentImage).where(DocumentImage.document_id == doc_id)).all()
    return {
        "document": d.model_dump(mode="json"),
        "pages": [p.model_dump(mode="json") for p in pages],
        "images": [i.model_dump(mode="json") for i in images],
    }


@router.get("/{doc_id}/file")
def download_document(
    doc_id: int,
    download: bool = False,
    user: User = Depends(media_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    from app.auth.acl import can_see_document

    """Serve the original PDF.

    By default the response is ``inline`` so the browser's PDF viewer renders
    it (and honours ``#page=N`` fragments). Pass ``?download=1`` to force the
    browser to save the file instead.
    """
    d = session.get(Document, doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    if not can_see_document(user, d):
        raise HTTPException(status_code=403, detail="forbidden")
    p = Path(d.path)
    if not _served_path_allowed(p, d, session):
        raise HTTPException(status_code=403, detail="path outside the document's source")
    if not p.exists():
        raise HTTPException(status_code=410, detail="file no longer available on disk")
    disposition = "attachment" if download else "inline"
    # Quote the filename for non-ASCII safety
    quoted = d.filename.replace('"', "")
    headers = {"Content-Disposition": f'{disposition}; filename="{quoted}"'}
    return FileResponse(str(p), media_type="application/pdf", headers=headers)


def _require_can_see(doc_id: int, user: User, session: Session) -> Document:
    """Load a document and enforce the per-document ACL, or 404/403.

    404 (not 403) for an invisible doc avoids confirming its existence to a
    user who can't see it — but a visible-but-forbidden case still 403s.
    """
    from app.auth.acl import can_see_document

    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    if not can_see_document(user, doc):
        raise HTTPException(status_code=404, detail="not found")
    return doc


@router.get("/compare/{doc_a}/{doc_b}")
async def compare(
    doc_a: int,
    doc_b: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    from dataclasses import asdict

    from app.services.compare import compare_documents

    # ACL: compare reveals full text + an LLM diff of BOTH documents, so the
    # caller must be allowed to see each one (else it's a cross-tenant leak).
    _require_can_see(doc_a, user, session)
    _require_can_see(doc_b, user, session)
    try:
        result = await compare_documents(doc_a, doc_b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return asdict(result)


@router.get("/{doc_id}/similar")
async def similar(
    doc_id: int,
    top_k: int = 10,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    from dataclasses import asdict

    from app.auth.acl import allowed_document_ids
    from app.services.similar import find_similar

    _require_can_see(doc_id, user, session)
    hits = await find_similar(doc_id, top_k=top_k)
    # Filter results to documents the caller may see (non-admins must not learn
    # foreign filenames / absolute paths via the similarity neighbours).
    from app.models import UserRole

    if user.role != UserRole.admin:
        visible = allowed_document_ids(user, session)
        hits = [h for h in hits if getattr(h, "document_id", None) in visible]
    return [asdict(h) for h in hits]


# Allowed pixel widths for ?w= page renders. Fixed buckets keep the render
# work and disk usage bounded (no ?w=99999 render-DoS) and let the browser's
# srcset pick the closest match for the actual display resolution.
PAGE_WIDTH_BUCKETS = (768, 1024, 1536, 2048, 3072)


def snap_width_bucket(w: int) -> int:
    """Smallest allowed bucket >= w (largest bucket when w exceeds them all)."""
    for b in PAGE_WIDTH_BUCKETS:
        if w <= b:
            return b
    return PAGE_WIDTH_BUCKETS[-1]


def _cache_file_ok(p: Path) -> bool:
    """A cached render counts only if non-empty (a kill mid-write can leave a
    truncated file; writes are atomic via os.replace, so size>0 means complete)."""
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _media_file_response(request: Request, path: Path, media_type: str):
    """FileResponse with ETag + ``Cache-Control: private, no-cache``.

    ``no-cache`` lets the browser STORE the bytes but forces a revalidation on
    every use — the conditional GET re-runs ``media_user`` (token max-age,
    ``is_active``, per-document ACL) and gets a 304, so authorization stays
    per-request while the multi-MB PNG body is never re-downloaded. A plain
    ``max-age`` would keep serving document content from the local cache after
    logout/deactivation with no server round-trip at all.
    """
    try:
        st = path.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    except OSError:
        etag = None
    headers = {"Cache-Control": "private, no-cache"}
    if etag:
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            from fastapi import Response

            return Response(status_code=304, headers=headers)
    return FileResponse(str(path), media_type=media_type, headers=headers)


@router.get("/{doc_id}/page/{page_no}/image")
def get_page_image(
    request: Request,
    doc_id: int,
    page_no: int,
    w: int | None = None,
    user: User = Depends(media_user),
    session: Session = Depends(get_session),
):
    from app.auth.acl import can_see_document

    # Enforce the per-document ACL before serving any rendered page (these PNGs
    # are document *content*) — mirror download_document. A media token encodes
    # only the user id, not a document scope, so the check must happen here.
    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    if not can_see_document(user, doc):
        raise HTTPException(status_code=403, detail="forbidden")

    # The scan's render pointer doubles as the offline fallback below.
    page = session.exec(
        select(DocumentPage).where(DocumentPage.document_id == doc_id, DocumentPage.page_number == page_no)
    ).first()

    if w is None and page and page.rendered_image_path and _cache_file_ok(Path(page.rendered_image_path)):
        # Legacy path (no width requested): reuse whatever render exists —
        # the OCR scan's PNG or a previous on-the-fly 2x render.
        return _media_file_response(request, Path(page.rendered_image_path), "image/png")

    from app.config import get_settings

    s = get_settings()
    cache_dir = s.cache_path / "pages" / str(doc_id)
    bucket = snap_width_bucket(max(1, w)) if w is not None else None
    # Bucketed renders use their own filenames; file existence IS the cache
    # (no DB pointer, so no write_session contention with the indexer). The
    # indexer purges these files when a re-scan sees a changed content hash.
    out = cache_dir / f"page_{page_no:04d}_w{bucket}.png" if bucket else cache_dir / f"page_{page_no:04d}.png"
    # Serve any existing render BEFORE requiring the original file — sources
    # on unplugged drives / unreachable shares are a normal state here.
    if _cache_file_ok(out):
        return _media_file_response(request, out, "image/png")

    src_path = Path(doc.path)
    if not _served_path_allowed(src_path, doc, session):
        # Never rasterize a file outside the document's source root (arbitrary
        # file read otherwise). Fall back to any app-generated render we have.
        if page and page.rendered_image_path and _cache_file_ok(Path(page.rendered_image_path)):
            return _media_file_response(request, Path(page.rendered_image_path), "image/png")
        raise HTTPException(status_code=403, detail="path outside the document's source")
    if not src_path.exists():
        # Original offline: fall back to whatever other render exists.
        if page and page.rendered_image_path and _cache_file_ok(Path(page.rendered_image_path)):
            return _media_file_response(request, Path(page.rendered_image_path), "image/png")
        legacy = cache_dir / f"page_{page_no:04d}.png"
        if _cache_file_ok(legacy):
            return _media_file_response(request, legacy, "image/png")
        raise HTTPException(status_code=410, detail="original file missing")

    try:
        import os

        import fitz

        cache_dir.mkdir(parents=True, exist_ok=True)
        d = fitz.open(str(src_path))
        try:
            if page_no < 1 or page_no > d.page_count:
                raise HTTPException(status_code=404, detail="page out of range")
            pg = d.load_page(page_no - 1)
            if bucket:
                # page.rect is rotation-aware, so width is the displayed width.
                zoom = bucket / pg.rect.width if pg.rect.width > 0 else 2.0
                zoom = max(0.1, min(zoom, 6.0))
                mat = fitz.Matrix(zoom, zoom)
            else:
                mat = fitz.Matrix(2, 2)
            pix = pg.get_pixmap(matrix=mat, alpha=False)
            # Atomic write: a process kill mid-write must not leave a truncated
            # PNG behind that existence-keyed caching would then serve forever.
            tmp = out.with_name(out.name + f".tmp{os.getpid()}")
            tmp.write_bytes(pix.tobytes("png"))
            os.replace(tmp, out)
        finally:
            d.close()
        if bucket is None and page is not None and page.id is not None:
            # Persist the cache pointer through the serialized write path:
            # this endpoint runs on a request worker thread and can fire
            # while a scan is indexing, so a lock-free write here would race
            # the indexer and hit "database is locked". The fitz render
            # above stays outside the lock.
            with write_session() as ws:
                p = ws.get(DocumentPage, page.id)
                if p is not None:
                    p.rendered_image_path = str(out)
                    ws.add(p)
        return _media_file_response(request, out, "image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"render failed: {e}")


@router.get("/{doc_id}/matches")
def get_match_rects(
    doc_id: int,
    q: str,
    pages: str = "",
    user: User = Depends(media_user),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Normalized highlight rectangles for the query's meaningful terms.

    ``pages`` is a comma-separated list of 1-based page numbers (capped).
    Returns ``{"rects": {"<page>": [[x, y, w, h], …]}}`` with fractions of the
    displayed page size — resolution-independent, so the viewer can overlay
    them on any cached render. Pages without a text layer yield no entries.
    """
    from app.auth.acl import can_see_document
    from app.services.page_matches import MAX_PAGES, match_rects_for_pages
    from app.utils.terms import meaningful_terms

    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    if not can_see_document(user, doc):
        raise HTTPException(status_code=403, detail="forbidden")
    page_nos: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        # isdecimal, not isdigit: '²' passes isdigit() but int() raises.
        if part.isdecimal() and len(part) <= 6:
            try:
                page_nos.append(int(part))
            except ValueError:
                continue
        if len(page_nos) >= MAX_PAGES:
            break
    terms = meaningful_terms(q)
    if not terms or not page_nos or not _served_path_allowed(Path(doc.path), doc, session):
        return {"rects": {}}
    if not Path(doc.path).exists():
        return {"rects": {}}
    rects = match_rects_for_pages(doc.path, page_nos, terms)
    return {"rects": {str(k): [list(r) for r in v] for k, v in rects.items()}}


@router.get("/{doc_id}/img/{image_id}")
def get_extracted_image(
    request: Request,
    doc_id: int,
    image_id: int,
    user: User = Depends(media_user),
    session: Session = Depends(get_session),
):
    """Serve an embedded image extracted from the PDF during a vision scan.

    Used by search/chat result cards to show the actual pictures (logos,
    figures) that live on a matched page. ACL-gated like download_document.
    """
    from app.auth.acl import can_see_document

    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    if not can_see_document(user, doc):
        raise HTTPException(status_code=403, detail="forbidden")
    img = session.get(DocumentImage, image_id)
    if not img or img.document_id != doc_id:
        raise HTTPException(status_code=404, detail="image not found")
    p = Path(img.cache_path)
    if not _served_path_allowed(p, doc, session):
        raise HTTPException(status_code=403, detail="path outside the document's source")
    if not p.exists():
        raise HTTPException(status_code=410, detail="image no longer available on disk")
    suffix = p.suffix.lower().lstrip(".") or "png"
    media = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
    return _media_file_response(request, p, media)
