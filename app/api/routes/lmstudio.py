"""LM Studio test endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.security import login_required
from app.llm import LMStudioClient
from app.models import User

router = APIRouter()


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


@router.post("/test")
async def test(body: TestBody, _user: User = Depends(login_required)) -> dict[str, Any]:
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
