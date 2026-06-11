"""Document-source CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required, require_admin
from app.database import get_session
from app.models import DocumentSource, SourceType, User
from app.services.watcher import is_watching, start_watcher, stop_watcher

router = APIRouter()


class SourceIn(BaseModel):
    name: str
    type: SourceType = SourceType.local
    path: str
    active: bool = True
    recursive: bool = True
    ignore_hidden: bool = True
    include_patterns: list[str] = ["*.pdf"]
    exclude_patterns: list[str] = []
    max_file_size_mb: int | None = None
    scan_interval_minutes: int | None = None


def _validate_source(body: SourceIn) -> None:
    """Confine a LOCAL source root server-side.

    A source root + glob patterns drive which host files get catalogued and
    then become downloadable through the document routes. Without limits an
    admin (or anyone replaying an admin credential) could point a source at a
    filesystem root or at the app's own data dir and exfiltrate ``secret.key``
    / ``trovato.db``. Remote (SMB/WebDAV/SFTP) paths are not local FS paths and
    are left to their provider.
    """
    from pathlib import Path

    from app.config import get_settings
    from app.utils.paths import is_under

    # Bound the pattern lists so they can't be abused as an unbounded payload.
    for patterns in (body.include_patterns, body.exclude_patterns):
        if len(patterns) > 50 or any(len(p) > 200 for p in patterns):
            raise HTTPException(status_code=400, detail="too many / too long glob patterns")

    if body.type != SourceType.local:
        return
    try:
        resolved = Path(body.path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="invalid source path")
    # A filesystem / drive root (C:\, /, \\server) is far too broad.
    if resolved.parent == resolved:
        raise HTTPException(status_code=400, detail="source path may not be a filesystem root")
    # Never let a source overlap the app data dir (it holds secret.key + the DB).
    data = get_settings().data_path.resolve()
    if resolved == data or is_under(resolved, data) or is_under(data, resolved):
        raise HTTPException(status_code=400, detail="source path overlaps the app data directory")


@router.get("")
def list_sources(
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    from app.auth.acl import filter_sources

    stmt = filter_sources(select(DocumentSource), user).order_by(DocumentSource.id)  # type: ignore
    rows = session.exec(stmt).all()
    return [r.model_dump(mode="json") for r in rows]


@router.post("")
def create_source(
    body: SourceIn,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _validate_source(body)
    src = DocumentSource(**body.model_dump(), owner_id=user.id)
    session.add(src)
    session.flush()
    return src.model_dump(mode="json")


@router.put("/{source_id}")
def update_source(
    source_id: int,
    body: SourceIn,
    _user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    _validate_source(body)
    src = session.get(DocumentSource, source_id)
    if not src:
        raise HTTPException(status_code=404, detail="not found")
    for k, v in body.model_dump().items():
        setattr(src, k, v)
    session.add(src)
    return src.model_dump(mode="json")


class CredentialsBody(BaseModel):
    base_url: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    private_key_path: str | None = None


@router.put("/{source_id}/credentials")
def set_credentials(
    source_id: int,
    body: CredentialsBody,
    _user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    src = session.get(DocumentSource, source_id)
    if not src:
        raise HTTPException(status_code=404, detail="not found")
    from app.utils.secret_store import put_secret

    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    ref = f"source-{source_id}"
    put_secret(ref, payload)
    src.credentials_ref = ref
    session.add(src)
    return {"credentials_ref": ref, "keys": list(payload.keys())}


@router.delete("/{source_id}/credentials")
def delete_credentials(
    source_id: int,
    _user: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    src = session.get(DocumentSource, source_id)
    if not src:
        raise HTTPException(status_code=404, detail="not found")
    from app.utils.secret_store import delete_secret

    if src.credentials_ref:
        delete_secret(src.credentials_ref)
        src.credentials_ref = None
        session.add(src)
    return {"status": "deleted"}


@router.post("/{source_id}/watch")
def watch(source_id: int, _user: User = Depends(require_admin)) -> dict[str, Any]:
    start_watcher(source_id)
    return {"watching": is_watching(source_id)}


@router.post("/{source_id}/unwatch")
def unwatch(source_id: int, _user: User = Depends(require_admin)) -> dict[str, Any]:
    stopped = stop_watcher(source_id)
    return {"stopped": stopped}


@router.delete("/{source_id}")
def delete_source(
    source_id: int,
    _user: User = Depends(require_admin),
) -> dict[str, str]:
    # A bare DELETE fails the moment the source has documents or scan-job
    # history (enforced FKs, no cascade). Tear the children down first.
    from app.services.sources import delete_source_cascade

    if not delete_source_cascade(source_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted"}
