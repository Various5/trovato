"""Search endpoints (hybrid)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.security import login_required
from app.models import User
from app.services.search_service import hybrid_search


router = APIRouter()


class SearchBody(BaseModel):
    query: str
    top_k: int = 15
    document_ids: Optional[list[int]] = None
    source_ids: Optional[list[int]] = None
    tags: Optional[list[str]] = None
    rerank: bool = False


@router.post("")
async def search(body: SearchBody, user: User = Depends(login_required)) -> list[dict[str, Any]]:
    hits = await hybrid_search(
        body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        source_ids=body.source_ids,
        tags=body.tags,
        rerank=body.rerank,
        user=user,
    )
    return [asdict(h) for h in hits]
