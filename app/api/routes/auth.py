"""Auth endpoints: login, logout, first-run, change-password, recovery."""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.rate_limit import is_locked, record_failure, record_success
from app.auth.security import (
    create_user,
    get_current_user,
    has_users,
    hash_password,
    login_session,
    logout_session,
    make_recovery_key,
    reset_password_with_recovery,
    session_fingerprint,
    verify_password,
)
from app.database import get_session
from app.models import User, UserRole
from app.services import audit

router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


class FirstRunBody(BaseModel):
    username: str
    password: str
    lmstudio_base_url: str | None = None


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class RecoveryBody(BaseModel):
    username: str
    recovery_key: str
    new_password: str


@router.get("/state")
def state(session: Session = Depends(get_session)) -> dict:
    return {"first_run": not has_users(session)}


@router.post("/first-run")
def first_run(body: FirstRunBody, request: Request, session: Session = Depends(get_session)) -> dict:
    if has_users(session):
        raise HTTPException(status_code=400, detail="already configured")
    user = create_user(session, username=body.username, password=body.password, role=UserRole.admin)
    if body.lmstudio_base_url:
        from app.config import get_settings, save_user_settings

        save_user_settings({"lmstudio_base_url": body.lmstudio_base_url})
        get_settings.cache_clear()
    recovery_key = make_recovery_key()
    user.recovery_key_hash = hash_password(recovery_key)
    session.add(user)
    session.flush()
    login_session(request, user)
    return {"id": user.id, "username": user.username, "recovery_key": recovery_key}


@router.post("/login")
def login(body: LoginBody, request: Request, session: Session = Depends(get_session)) -> dict:
    ip = request.client.host if request.client else "unknown"
    locked, retry_after = is_locked(ip, body.username)
    if locked:
        audit.log("auth.login.locked", payload={"ip": ip, "username": body.username})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many attempts; retry in {int(retry_after)}s",
            headers={"Retry-After": str(int(retry_after))},
        )

    user = session.exec(select(User).where(User.username == body.username)).first()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        was_locked, lockout = record_failure(ip, body.username)
        audit.log(
            "auth.login.failed",
            payload={"ip": ip, "username": body.username, "locked": was_locked},
        )
        if was_locked:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"account locked for {int(lockout)}s",
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    record_success(ip, body.username)
    from datetime import datetime

    user.last_login_at = datetime.now(UTC)
    session.add(user)
    login_session(request, user)
    audit.log("auth.login.success", user_id=user.id, payload={"ip": ip})
    return {"id": user.id, "username": user.username, "role": user.role.value}


@router.post("/logout")
def logout(request: Request) -> dict:
    uid = request.session.get("uid")
    logout_session(request)
    audit.log("auth.logout", user_id=uid)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role.value}


@router.post("/change-password")
def change_password(
    body: ChangePasswordBody,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="old password incorrect")
    user.password_hash = hash_password(body.new_password)
    session.add(user)
    session.flush()
    # Revoke every OTHER session (their stamped fingerprint is now stale) but keep
    # this caller's session valid by re-stamping it with the new fingerprint.
    request.session["pwv"] = session_fingerprint(user)
    audit.log("auth.password.changed", user_id=user.id)
    return {"ok": True}


@router.post("/recover")
def recover(body: RecoveryBody, request: Request, session: Session = Depends(get_session)) -> dict:
    # Rate-limit like login — recover is unauthenticated and each attempt forces
    # a memory-hard Argon2 verify, so without throttling it's a CPU/RAM DoS.
    ip = request.client.host if request.client else "unknown"
    bucket = "recover:" + body.username
    locked, retry_after = is_locked(ip, bucket)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many attempts; retry in {int(retry_after)}s",
            headers={"Retry-After": str(int(retry_after))},
        )
    ok = reset_password_with_recovery(
        session,
        username=body.username,
        recovery_key=body.recovery_key,
        new_password=body.new_password,
    )
    if not ok:
        record_failure(ip, bucket)
        raise HTTPException(status_code=400, detail="invalid recovery")
    record_success(ip, bucket)
    return {"ok": True}
