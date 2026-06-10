"""LM Studio test endpoints."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.security import login_required
from app.llm import LMStudioClient
from app.models import User

router = APIRouter()


def _reject_metadata_target(url: str | None) -> None:
    """Block a base_url that points at a literal link-local IP (169.254.0.0/16 /
    fe80::/10) — the cloud-metadata range, never a legitimate LM Studio host.
    Loopback/private IPs and hostnames are allowed (those ARE the normal targets:
    localhost and LAN boxes), so this only stops the clear SSRF-to-metadata case."""
    host = (urlsplit(url or "").hostname or "").strip()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # hostname, not a literal IP
    if ip.is_link_local:
        raise HTTPException(status_code=400, detail="refused: link-local/metadata address")


class TestBody(BaseModel):
    base_url: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None


@router.get("/ping")
async def ping(_user: User = Depends(login_required)) -> dict[str, Any]:
    client = LMStudioClient()
    ok = await client.ping()
    return {"ok": ok, "base_url": client.base_url}


@router.get("/models")
async def models(_user: User = Depends(login_required)) -> list[dict[str, Any]]:
    client = LMStudioClient()
    try:
        return await client.list_models()
    except Exception as e:
        return [{"error": str(e)}]


@router.get("/models/categorized")
async def models_categorized(_user: User = Depends(login_required)) -> dict[str, Any]:
    """Same as /models but pre-classified into chat / embedding / vision buckets.

    Classification is heuristic on the model id — most LM Studio listings don't
    expose the model type, so we look at name conventions.
    """
    client = LMStudioClient()
    try:
        raw = await client.list_models()
    except Exception as e:
        return {"error": str(e), "all": [], "chat": [], "embedding": [], "vision": []}

    chat: list[str] = []
    embedding: list[str] = []
    vision: list[str] = []
    all_ids: list[str] = []

    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("model")
        if not mid:
            continue
        all_ids.append(mid)
        lid = mid.lower()
        is_embedding = any(k in lid for k in ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic"))
        is_vision = any(k in lid for k in ("-vl", "vision", "llava", "moondream", "internvl"))
        if is_embedding:
            embedding.append(mid)
        if is_vision:
            vision.append(mid)
        if not is_embedding:
            # everything that's not strictly an embedding model can serve as chat
            chat.append(mid)

    return {
        "all": all_ids,
        "chat": chat,
        "embedding": embedding,
        "vision": vision,
    }


@router.post("/models/auto-pick")
async def auto_pick(_user: User = Depends(login_required)) -> dict[str, Any]:
    """Pick a default chat / embedding / vision model from what's currently
    loaded in LM Studio and persist the choice to settings.json."""
    client = LMStudioClient()
    try:
        raw = await client.list_models()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    chat = embedding = vision = ""
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("model") or ""
        if not mid:
            continue
        lid = mid.lower()
        if not embedding and any(
            k in lid for k in ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic")
        ):
            embedding = mid
            continue
        if not vision and any(k in lid for k in ("-vl", "vision", "llava", "moondream", "internvl")):
            vision = mid
            continue
        if not chat and not any(
            k in lid for k in ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic")
        ):
            chat = mid

    from app.config import get_settings, save_user_settings

    updates: dict[str, Any] = {}
    if chat:
        updates["chat_model"] = chat
    if embedding:
        updates["embedding_model"] = embedding
    if vision:
        updates["vision_model"] = vision
    if updates:
        save_user_settings(updates)
        get_settings.cache_clear()

    return {
        "ok": True,
        "chat_model": chat or None,
        "embedding_model": embedding or None,
        "vision_model": vision or None,
        "applied": list(updates.keys()),
    }


@router.get("/models/recommend")
async def models_recommend(_user: User = Depends(login_required)) -> dict[str, Any]:
    """Recommend the best already-downloaded model per role for this machine.

    Roles with no suitable local model carry a ``suggestion`` (an ``lms get``
    target + rough size) so the UI can offer an ask-first download.
    """
    from app.config import get_settings
    from app.services import lms_cli
    from app.services.hardware import active_tuning
    from app.services.model_advisor import recommend

    client = LMStudioClient()
    try:
        available = await client.list_downloaded()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    tier = active_tuning().tier
    pref = getattr(get_settings(), "model_quality", "balanced")
    plan = recommend(available, tier=tier, chat_preference=pref)
    return {
        "ok": True,
        "tier": tier,
        "chat_preference": pref,
        "lms_available": lms_cli.is_available(),
        "roles": {
            c.role: {
                "model": c.model,
                "suggestion": c.suggestion,
                "size_gb": c.size_gb,
            }
            for c in plan.choices
        },
    }


class ApplyBody(BaseModel):
    auto: bool = False  # when true, compute picks from the advisor
    chat_model: str | None = None
    embedding_model: str | None = None
    vision_model: str | None = None


@router.post("/models/apply")
async def models_apply(body: ApplyBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    """Persist model choices to settings and warm them up in LM Studio.

    With ``auto=true`` the picks come from the advisor; otherwise the explicit
    ``*_model`` fields are applied. Returns which roles were warmed.
    """
    from app.config import get_settings, save_user_settings
    from app.llm import warm_up_configured

    updates: dict[str, Any] = {}
    if body.auto:
        client = LMStudioClient()
        try:
            available = await client.list_downloaded()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        from app.services.hardware import active_tuning
        from app.services.model_advisor import recommend

        pref = getattr(get_settings(), "model_quality", "balanced")
        plan = recommend(available, tier=active_tuning().tier, chat_preference=pref)
        updates = plan.picks()
    else:
        if body.chat_model:
            updates["chat_model"] = body.chat_model
        if body.embedding_model:
            updates["embedding_model"] = body.embedding_model
        if body.vision_model:
            updates["vision_model"] = body.vision_model

    if updates:
        save_user_settings(updates)
        get_settings.cache_clear()

    warm = await warm_up_configured()
    return {
        "ok": True,
        "applied": updates,
        "warmup": {k: {"ok": v[0], "msg": v[1]} for k, v in warm.items()},
    }


class DownloadBody(BaseModel):
    model: str


@router.post("/models/download")
async def models_download(body: DownloadBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    """Download a model via the ``lms`` CLI (``lms get -y``). May take minutes."""
    from app.services import lms_cli

    if not lms_cli.is_available():
        return {"ok": False, "error": "lms CLI not found — install LM Studio's CLI to download models"}
    ok, out = await lms_cli.download(body.model)
    return {"ok": ok, "output": out[-2000:]}


@router.post("/test")
async def test(body: TestBody, _user: User = Depends(login_required)) -> dict[str, Any]:
    _reject_metadata_target(body.base_url)
    client = LMStudioClient(base_url=body.base_url)
    result: dict[str, Any] = {"base_url": client.base_url}
    result["ping"] = await client.ping()
    if body.chat_model:
        try:
            answer = await client.chat(
                [{"role": "user", "content": "Say 'pong' if you read this."}],
                model=body.chat_model,
                max_tokens=20,
            )
            result["chat"] = {"ok": True, "sample": answer[:120]}
        except Exception as e:
            result["chat"] = {"ok": False, "error": str(e)}
    if body.embedding_model:
        try:
            vecs = await client.embed(["hello world"], model=body.embedding_model)
            result["embedding"] = {"ok": bool(vecs), "dim": len(vecs[0]) if vecs else 0}
        except Exception as e:
            result["embedding"] = {"ok": False, "error": str(e)}
    return result
