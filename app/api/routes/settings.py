"""Settings endpoints — read/write user-editable settings + memory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.security import login_required
from app.config import get_settings, load_user_settings, save_user_settings
from app.database import get_session
from app.llm.lmstudio import reset_client_cache
from app.models import User, UserMemory, UserSetting

router = APIRouter()


_ALLOWED_KEYS = {
    "lmstudio_base_url",
    "chat_model",
    "vision_model",
    "embedding_model",
    "vision_language",
    "ocr_backend",
    "tesseract_cmd",
    "ocr_lang",
    "chunk_size",
    "chunk_overlap",
    "ocr_min_text_chars",
    "performance_profile",
    "parallel_workers",
    "allow_lan",
    "log_level",
}

# Keys whose value is constrained to a fixed set — validated server-side so a
# bad PATCH can't poison settings.json (the UI only enforces this client-side).
from app.services.hardware import PROFILES as _PROFILES

_ENUM_KEYS: dict[str, set[str]] = {"performance_profile": set(_PROFILES)}


class SettingsPatch(BaseModel):
    updates: dict[str, Any]


class UserSettingsPatch(BaseModel):
    theme: str | None = None
    language: str | None = None
    answer_length: str | None = None
    memory_enabled: bool | None = None


class MemoryEntry(BaseModel):
    key: str
    value: str
    sensitive: bool = False


@router.get("")
def read_settings(_user: User = Depends(login_required)) -> dict[str, Any]:
    s = get_settings()
    user_overlay = load_user_settings()
    public = {
        "lmstudio_base_url": s.lmstudio_base_url,
        "chat_model": s.chat_model,
        "vision_model": s.vision_model,
        "embedding_model": s.embedding_model,
        "vision_language": s.vision_language,
        "tesseract_cmd": s.tesseract_cmd,
        "ocr_lang": s.ocr_lang,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "ocr_min_text_chars": s.ocr_min_text_chars,
        "performance_profile": s.performance_profile,
        "parallel_workers": s.parallel_workers,
        "allow_lan": s.allow_lan,
        "log_level": s.log_level,
        "data_dir": str(s.data_path),
        "db_path": str(s.db_path),
        "chroma_path": str(s.chroma_path),
    }
    public["_overlay_keys"] = sorted(user_overlay.keys())
    return public


@router.patch("")
def update_settings(body: SettingsPatch, _user: User = Depends(login_required)) -> dict[str, Any]:
    bad = set(body.updates) - _ALLOWED_KEYS
    if bad:
        raise HTTPException(status_code=400, detail=f"keys not allowed: {bad}")
    for key, allowed in _ENUM_KEYS.items():
        val = body.updates.get(key)
        if val is not None and val not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be one of {sorted(allowed)}, got {val!r}",
            )
    save_user_settings(body.updates)
    get_settings.cache_clear()
    reset_client_cache()
    return read_settings()  # type: ignore[arg-type]


# ---- per-user settings (theme, language, ...)


@router.get("/me")
def my_settings(
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
    if not s:
        s = UserSetting(user_id=user.id)
        session.add(s)
        session.flush()
    return s.model_dump(mode="json")


@router.patch("/me")
def patch_my_settings(
    body: UserSettingsPatch,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    s = session.exec(select(UserSetting).where(UserSetting.user_id == user.id)).first()
    if not s:
        s = UserSetting(user_id=user.id)
        session.add(s)
        session.flush()
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    session.add(s)
    return s.model_dump(mode="json")


# ---- memory


@router.get("/memory")
def list_memory(
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = session.exec(select(UserMemory).where(UserMemory.user_id == user.id)).all()
    return [r.model_dump(mode="json") for r in rows]


@router.post("/memory")
def add_memory(
    body: MemoryEntry,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    m = UserMemory(
        user_id=user.id,  # type: ignore[arg-type]
        key=body.key,
        value=body.value,
        sensitive=body.sensitive,
        confirmed=not body.sensitive,
    )
    session.add(m)
    session.flush()
    return m.model_dump(mode="json")


@router.delete("/memory/{memory_id}")
def delete_memory(
    memory_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    m = session.get(UserMemory, memory_id)
    if not m or m.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    session.delete(m)
    return {"status": "deleted"}


@router.post("/memory/{memory_id}/confirm")
def confirm_memory(
    memory_id: int,
    user: User = Depends(login_required),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    m = session.get(UserMemory, memory_id)
    if not m or m.user_id != user.id:
        raise HTTPException(status_code=404, detail="not found")
    m.confirmed = True
    session.add(m)
    return m.model_dump(mode="json")
