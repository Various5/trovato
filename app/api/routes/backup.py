"""Backup / Restore endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.security import login_required
from app.backup import BACKUP_COMPONENTS, create_backup, list_backups, restore_backup
from app.models import User


router = APIRouter()


class CreateBody(BaseModel):
    components: list[str]
    encrypt_password: str | None = None
    include_originals: bool = False


class RestoreBody(BaseModel):
    archive_path: str
    components: list[str] | None = None
    password: str | None = None


@router.get("/components")
def components() -> list[str]:
    return BACKUP_COMPONENTS


@router.get("")
def listing(_user: User = Depends(login_required)) -> list[dict[str, Any]]:
    return list_backups()


@router.post("")
def create(body: CreateBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    res = create_backup(
        body.components,
        encrypt_password=body.encrypt_password or None,
        include_originals=body.include_originals,
    )
    return {
        "path": str(res.path),
        "size_bytes": res.size_bytes,
        "components": res.components,
        "encrypted": res.encrypted,
    }


@router.post("/restore")
def restore(body: RestoreBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    p = Path(body.archive_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="archive not found")
    return restore_backup(p, components=body.components, password=body.password)
