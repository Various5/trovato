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
