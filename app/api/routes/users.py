"""User CRUD endpoints — admin-only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import create_user, hash_password, require_admin
from app.database import get_session
from app.models import User, UserRole
from app.services import audit


router = APIRouter()


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.user


class UserPatchBody(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = None


@router.get("")
def list_users(
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = session.exec(select(User).order_by(User.id)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role.value,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        }
        for u in rows
    ]


@router.post("")
def add_user(
    body: UserCreateBody,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        user = create_user(
            session, username=body.username, password=body.password, role=body.role
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log("user.created", user_id=admin.id, payload={"new_user": user.id})
    return {"id": user.id, "username": user.username, "role": user.role.value}


@router.patch("/{user_id}")
def patch_user(
    user_id: int,
    body: UserPatchBody,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="not found")
    changes: dict[str, Any] = {}
    if body.role is not None:
        user.role = body.role
        changes["role"] = body.role.value
    if body.is_active is not None:
        user.is_active = body.is_active
        changes["is_active"] = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    session.add(user)
    audit.log("user.patched", user_id=admin.id, payload={"target": user_id, **changes})
    return {"id": user.id, "username": user.username, "role": user.role.value}


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="not found")
    user.is_active = False  # soft-delete to preserve audit trail and FK refs
    session.add(user)
    audit.log("user.deactivated", user_id=admin.id, payload={"target": user_id})
    return {"status": "deactivated"}
