"""LLM-as-reranker.

Given a query and a list of candidate hits, ask the local chat model to assign
a relevance score (0–10) to each snippet. The model output is parsed loosely;
any missing parses keep the original score so a misbehaving model can't make
results worse than the un-reranked baseline.

This runs *after* hybrid_search and only on the top-N candidates to keep
latency bounded.
"""

from __future__ import annotations

import json
import re
from typing import List

from app.llm import LMStudioError, get_client
from app.services.search_service import SearchHit
from app.utils.logging import logger


_SYS = (
    "You are a search reranker. For each NUMBERED snippet, output a JSON array "
    "of objects: [{\"n\": <number>, \"score\": <0-10>}]. Higher is better. "
    "Do not output anything else."
)


_JSON_BLOCK_RX = re.compile(r"\[.*\]", re.S)


async def rerank(query: str, hits: List[SearchHit], *, max_candidates: int = 12) -> List[SearchHit]:
    if not hits or len(hits) < 2:
        return hits
    candidates = hits[:max_candidates]
    prompt_lines = [f"QUERY: {query}", "", "SNIPPETS:"]
    for i, h in enumerate(candidates, start=1):
        prompt_lines.append(f"[{i}] ({h.filename} p.{h.page_from}) {h.snippet[:600]}")
    prompt = "\n".join(prompt_lines) + "\n\nReturn the JSON array now."

    try:
        client = get_client()
        raw = await client.chat(
            [{"role": "system", "content": _SYS}, {"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
        )
    except LMStudioError as e:
        logger.debug("rerank skipped (no LM Studio): {}", e)
        return hits
    except Exception as e:
        logger.warning("rerank failed: {}", e)
        return hits

    scores: dict[int, float] = {}
    try:
        m = _JSON_BLOCK_RX.search(raw or "")
        if m:
            parsed = json.loads(m.group(0))
            for entry in parsed:
                n = int(entry.get("n"))
                sc = float(entry.get("score"))
                if 0 <= sc <= 10:
                    scores[n] = sc
    except Exception as e:
        logger.debug("rerank parse failed: {}", e)

    if not scores:
        return hits

    # Blend: 60 % rerank, 40 % original
    out = list(hits)
    for idx, h in enumerate(candidates):
        n = idx + 1
        if n in scores:
            blended = 0.6 * (scores[n] / 10.0) + 0.4 * h.score
            out[idx] = SearchHit(
                chunk_id=h.chunk_id,
                document_id=h.document_id,
                filename=h.filename,
                path=h.path,
                page_from=h.page_from,
                page_to=h.page_to,
                snippet=h.snippet,
                score=blended,
                source=h.source,
                tags=h.tags,
            )
    out.sort(key=lambda h: h.score, reverse=True)
    return out
