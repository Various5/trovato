"""Authentication: Argon2 hashing, session-cookie helpers, FastAPI deps."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import URLSafeTimedSerializer
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import User, UserRole, UserSetting
from app.utils.logging import logger

_ph = PasswordHasher()
SESSION_USER_KEY = "uid"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception as e:  # malformed hash etc.
        logger.warning("verify_password failed: {}", e)
        return False


# ---------------------------------------------------------------------------
# User lifecycle
# ---------------------------------------------------------------------------


def has_users(session: Session) -> bool:
    return session.exec(select(User).limit(1)).first() is not None


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: UserRole = UserRole.admin,
) -> User:
    if session.exec(select(User).where(User.username == username)).first():
        raise ValueError(f"user '{username}' already exists")
    user = User(
        username=username.strip(),
        password_hash=hash_password(password),
        role=role,
        recovery_key_hash=hash_password(secrets.token_urlsafe(24)),
    )
    session.add(user)
    session.flush()
    session.add(UserSetting(user_id=user.id))
    return user


def set_password(session: Session, user: User, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
    session.add(user)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def session_fingerprint(user: User) -> str:
    """A short, stable fingerprint of the user's current password hash, stamped
    into the session at login. The Argon2 hash is salted+unique per password, so
    changing the password changes this — and every session stamped with the old
    value stops validating. Gives password-change session revocation with no
    schema change and no extra query (the User row is already loaded)."""
    return (user.password_hash or "")[-24:]


def login_session(request: Request, user: User) -> None:
    request.session[SESSION_USER_KEY] = user.id
    request.session["pwv"] = session_fingerprint(user)


def logout_session(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)
    request.session.pop("pwv", None)


def current_user_id(request: Request) -> int | None:
    try:
        return request.session.get(SESSION_USER_KEY)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Signed media tokens — PDF / page-image access from new tabs and <img> tags
# ---------------------------------------------------------------------------

_MEDIA_SALT = "ldi-media-access-v1"


def make_media_token(user_id: int) -> str:
    """Return a signed, time-limited token authorizing media access.

    Browser-issued requests for ``/api/documents/{id}/file`` (opened in a new
    tab) and ``/page/{n}/image`` (an ``<img>`` src) don't reliably carry the
    NiceGUI/Starlette session, so the UI appends ``?t=<token>`` and the
    endpoints accept it via ``media_user`` (app/api/routes/documents.py).
    """
    s = URLSafeTimedSerializer(get_settings().secret_key, salt=_MEDIA_SALT)
    return s.dumps({"uid": int(user_id)})


def verify_media_token(token: str, max_age: int = 86400) -> int | None:
    """Return the user id from a valid, unexpired media token, else ``None``."""
    try:
        s = URLSafeTimedSerializer(get_settings().secret_key, salt=_MEDIA_SALT)
        data = s.loads(token, max_age=max_age)
        return int(data["uid"])
    except Exception:
        return None


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
    user = session.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    # Revoke sessions stamped before a password change (or pre-update sessions
    # that lack the fingerprint entirely).
    if request.session.get("pwv") != session_fingerprint(user):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session expired")
    return user


def login_required(request: Request, session: Session = Depends(get_session)) -> User:
    return get_current_user(request, session)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def make_recovery_key() -> str:
    """Return a one-shot recovery key (caller must show this to the user once)."""
    return secrets.token_urlsafe(24)


def reset_password_with_recovery(
    session: Session, *, username: str, recovery_key: str, new_password: str
) -> bool:
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or not user.recovery_key_hash:
        return False
    if not verify_password(recovery_key, user.recovery_key_hash):
        return False
    user.password_hash = hash_password(new_password)
    user.recovery_key_hash = hash_password(make_recovery_key())
    session.add(user)
    return True


__all__ = [
    "SESSION_USER_KEY",
    "create_user",
    "current_user_id",
    "get_current_user",
    "has_users",
    "hash_password",
    "login_required",
    "login_session",
    "logout_session",
    "make_media_token",
    "make_recovery_key",
    "verify_media_token",
    "require_admin",
    "reset_password_with_recovery",
    "session_fingerprint",
    "set_password",
    "verify_password",
]


# settings reference to silence linter if unused above
_ = get_settings
