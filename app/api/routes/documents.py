"""Document listing, details, and file streaming."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlmodel import Session, select

from app.auth.security import current_user_id, login_required, verify_media_token
from app.database import get_session, write_session
from app.models import Document, DocumentImage, DocumentPage, User

router = APIRouter()


def media_user(
    request: Request,
    t: str | None = None,
    session: Session = Depends(get_session),
) -> User:
    """Auth for media URLs: accept the session cookie OR a signed ``?t=`` token.

    PDF "open in browser" (new tab) and page-image previews (``<img>`` src) can't
    rely on the NiceGUI session being carried, so the UI appends a signed token.
    """
    uid = current_user_id(request)
    if uid is None and t:
        uid = verify_media_token(t)
    if uid is None:
        raise HTTPException(status_code=401, detail="login required")
    user = session.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="login required")
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
    if not p.exists():
        raise HTTPException(status_code=410, detail="file no longer available on disk")
    disposition = "attachment" if download else "inline"
    # Quote the filename for non-ASCII safety
    quoted = d.filename.replace('"', "")
    headers = {"Content-Disposition": f'{disposition}; filename="{quoted}"'}
    return FileResponse(str(p), media_type="application/pdf", headers=headers)


@router.get("/compare/{doc_a}/{doc_b}")
async def compare(
    doc_a: int,
    doc_b: int,
    _user: User = Depends(login_required),
) -> dict[str, Any]:
    from dataclasses import asdict

    from app.services.compare import compare_documents

    try:
        result = await compare_documents(doc_a, doc_b)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return asdict(result)


@router.get("/{doc_id}/similar")
async def similar(
    doc_id: int,
    top_k: int = 10,
    _user: User = Depends(login_required),
) -> list[dict[str, Any]]:
    from dataclasses import asdict

    from app.services.similar import find_similar

    hits = await find_similar(doc_id, top_k=top_k)
    return [asdict(h) for h in hits]


@router.get("/{doc_id}/page/{page_no}/image")
def get_page_image(
    doc_id: int,
    page_no: int,
    user: User = Depends(media_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    from app.auth.acl import can_see_document

    # Enforce the per-document ACL before serving any rendered page (these PNGs
    # are document *content*) — mirror download_document. A media token encodes
    # only the user id, not a document scope, so the check must happen here.
    doc = session.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    if not can_see_document(user, doc):
        raise HTTPException(status_code=403, detail="forbidden")

    page = session.exec(
        select(DocumentPage).where(DocumentPage.document_id == doc_id, DocumentPage.page_number == page_no)
    ).first()
    if page and page.rendered_image_path and Path(page.rendered_image_path).exists():
        return FileResponse(page.rendered_image_path, media_type="image/png")

    # On-the-fly render
    src_path = Path(doc.path)
    if not src_path.exists():
        raise HTTPException(status_code=410, detail="original file missing")
    try:
        import fitz

        from app.config import get_settings

        s = get_settings()
        cache_dir = s.cache_path / "pages" / str(doc_id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"page_{page_no:04d}.png"
        if not out.exists():
            d = fitz.open(str(src_path))
            try:
                if page_no < 1 or page_no > d.page_count:
                    raise HTTPException(status_code=404, detail="page out of range")
                mat = fitz.Matrix(2, 2)
                pix = d.load_page(page_no - 1).get_pixmap(matrix=mat, alpha=False)
                out.write_bytes(pix.tobytes("png"))
            finally:
                d.close()
            if page is not None and page.id is not None:
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
        return FileResponse(str(out), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"render failed: {e}")
