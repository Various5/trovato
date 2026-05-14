"""LM Studio client — OpenAI-compatible REST over httpx.

Models are *configurable*; defaults come from settings/.env. The same base URL
serves chat completions, embeddings, and vision (via image_url content parts).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.utils.logging import logger


class LMStudioError(RuntimeError):
    pass


class LMStudioClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.lmstudio_base_url).rstrip("/")
        self.api_key = api_key or s.lmstudio_api_key or "lm-studio"
        self.timeout = timeout

    # ---- helpers ----------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=self.timeout)

    # ---- discovery --------------------------------------------------------

    async def ping(self) -> bool:
        try:
            async with await self._client() as c:
                r = await c.get("/models")
                return r.status_code == 200
        except Exception as e:
            logger.debug("LM Studio ping failed: {}", e)
            return False

    async def list_models(self) -> list[dict[str, Any]]:
        """List models exposed by the server.

        Tries the OpenAI-compatible ``/v1/models`` first; if it returns an
        empty list, falls back to LM Studio's native ``/api/v0/models`` which
        often shows additional loaded models (embeddings in particular) that
        the OpenAI surface omits.
        """
        out: list[dict[str, Any]] = []
        async with await self._client() as c:
            try:
                r = await c.get("/models")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        out = list(data.get("data", []) or [])
                    elif isinstance(data, list):
                        out = list(data)
            except Exception as e:
                logger.debug("/v1/models failed: {}", e)

            if not out:
                # LM Studio native (base_url is .../v1, so go up one level)
                try:
                    base = str(c.base_url).rstrip("/")
                    if base.endswith("/v1"):
                        native = base[: -len("/v1")] + "/api/v0/models"
                    else:
                        native = base + "/api/v0/models"
                    r2 = await c.get(native)
                    if r2.status_code == 200:
                        data2 = r2.json()
                        items: list[dict[str, Any]] = []
                        if isinstance(data2, dict):
                            items = list(data2.get("data", []) or data2.get("models", []) or [])
                        elif isinstance(data2, list):
                            items = list(data2)
                        # Normalise: native API uses ``id``, sometimes nested
                        for it in items:
                            if isinstance(it, dict):
                                if "id" not in it and "model_key" in it:
                                    it["id"] = it["model_key"]
                                if "id" not in it and "name" in it:
                                    it["id"] = it["name"]
                        out = items
                except Exception as e:
                    logger.debug("/api/v0/models fallback failed: {}", e)
        return out

    # ---- chat -------------------------------------------------------------

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.5, max=4))
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        stream: bool = False,
    ) -> str:
        model = model or get_settings().chat_model
        if not model:
            raise LMStudioError("no chat model configured (Settings → Chat model)")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        async with await self._client() as c:
            r = await c.post("/chat/completions", json=payload)
            if r.status_code >= 400:
                raise LMStudioError(f"chat failed: {r.status_code} {r.text[:300]}")
            data = r.json()
            return data["choices"][0]["message"]["content"]

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        model = model or get_settings().chat_model
        if not model:
            raise LMStudioError("no chat model configured")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        async with await self._client() as c:
            async with c.stream("POST", "/chat/completions", json=payload) as r:
                if r.status_code >= 400:
                    text = await r.aread()
                    raise LMStudioError(f"stream failed: {r.status_code} {text[:300]!r}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    chunk = line[6:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        import json as _json

                        delta = _json.loads(chunk)["choices"][0].get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
                    except Exception:
                        continue

    # ---- embeddings -------------------------------------------------------

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=0.3, max=2),
        # Don't retry on user-error conditions (model not configured, 4xx, etc.)
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    async def embed(self, texts: Iterable[str], *, model: str | None = None) -> list[list[float]]:
        model = model or get_settings().embedding_model
        if not model:
            raise LMStudioError("no embedding model configured (Settings → Embedding model)")
        items = [t for t in texts if t and t.strip()]
        if not items:
            return []

        # Different LM Studio versions / backends expose embeddings on
        # different paths. Try the most likely candidates in order:
        #   1. /embeddings        — OpenAI standard on the existing /v1 base
        #   2. /api/v0/embeddings — LM Studio native API (sibling of /v1)
        #   3. /embedding         — old llama.cpp server (singular)
        #
        # Some llama.cpp builds bound inside LM Studio return HTTP 200 with
        # ``{"error": "Unexpected endpoint..."}`` for the wrong path, instead
        # of a 404. Treat any payload that contains a top-level ``error`` key
        # without ``data`` as "wrong path, try the next one".
        async with await self._client() as c:
            base = str(c.base_url).rstrip("/")
            sibling_native = base[: -len("/v1")] + "/api/v0/embeddings" if base.endswith("/v1") else None
            attempts: list[tuple[str, str]] = []
            if sibling_native:
                # LM Studio's native API is the most reliable on builds that
                # don't ship the OpenAI shim's /v1/embeddings — try it first.
                attempts.append(("native", sibling_native))
            attempts += [
                ("relative", "/embeddings"),
                ("relative-singular", "/embedding"),
            ]

            data: Any = None
            attempt_log: list[str] = []
            for kind, ep in attempts:
                try:
                    r = await c.post(ep, json={"model": model, "input": items})
                except Exception as e:
                    attempt_log.append(f"{kind}({ep}): {type(e).__name__}: {e}")
                    continue
                attempt_log.append(f"{kind}({ep}): {r.status_code}")
                if r.status_code == 404:
                    continue
                if r.status_code >= 400:
                    # 5xx / 400 — surface immediately with raw body
                    raise LMStudioError(f"embeddings failed: {r.status_code} {r.text[:300]}")
                try:
                    candidate = r.json()
                except Exception as e:
                    raise LMStudioError(f"invalid embeddings JSON: {e}") from None
                # Treat "200 OK + error-only payload" as a wrong-path response
                # (llama.cpp's "Returning 200 anyway" quirk).
                if (
                    isinstance(candidate, dict)
                    and "error" in candidate
                    and not candidate.get("data")
                    and not candidate.get("embeddings")
                ):
                    attempt_log[-1] += f" (200/error: {str(candidate.get('error'))[:80]})"
                    continue
                data = candidate
                break

            if data is None:
                raise LMStudioError(
                    "no embeddings endpoint responded with vectors. " f"Attempts: {' | '.join(attempt_log)}"
                )

        # The OpenAI standard shape is {"data": [{"embedding": [..]}]}, but
        # some llama.cpp-based backends inside LM Studio emit alternatives:
        #   - bare list-of-lists
        #   - {"embeddings": [[..]]}
        #   - {"data": [[..]]}  (no "embedding" key)
        # Handle all of them — and fall back to a clear error rather than
        # KeyError.
        rows: list[Any]
        if isinstance(data, dict):
            if "error" in data:
                raise LMStudioError(f"embeddings error: {data['error']}")
            rows = data.get("data") or data.get("embeddings") or []
            if not rows:
                raise LMStudioError(f"empty embeddings response (keys: {sorted(data)})")
        elif isinstance(data, list):
            rows = data
        else:
            raise LMStudioError(f"unexpected response type: {type(data).__name__}")

        results: list[list[float]] = []
        for row in rows:
            if isinstance(row, dict):
                vec = row.get("embedding") or row.get("vector")
                if vec is None:
                    raise LMStudioError(f"row missing 'embedding': keys={sorted(row)[:5]}")
                results.append(list(vec))
            elif isinstance(row, list):
                results.append(list(row))
            else:
                raise LMStudioError(f"unexpected row type: {type(row).__name__}")
        return results

    # ---- preflight --------------------------------------------------------

    async def preflight_embed(self, model: str | None = None) -> tuple[bool, str]:
        """Try a single embedding to verify connectivity + model availability.

        Returns ``(ok, message)`` with a human-readable diagnostic. Never
        raises — designed to be called from a "would this scan even work?"
        gate before tying up minutes on retries.
        """
        model = model or get_settings().embedding_model
        if not model:
            return False, "No embedding model configured (Settings → Embedding model)"
        try:
            vecs = await self.embed(["preflight"], model=model)
        except LMStudioError as e:
            msg = str(e)
            if "no embeddings endpoint responded" in msg:
                return (
                    False,
                    "LM Studio does not expose an /embeddings endpoint for this "
                    "model. In LM Studio: Developer → load the model with type "
                    f"'Embedding' (not Chat). Raw: {msg[:200]}",
                )
            return False, msg
        except httpx.ConnectError:
            return (
                False,
                f"Cannot reach {self.base_url}. Is LM Studio running and its " "local server enabled?",
            )
        except httpx.TimeoutException:
            return False, f"Timed out talking to {self.base_url}"
        except Exception as e:
            return False, f"Embedding probe failed: {type(e).__name__}: {e}"
        if not vecs or not vecs[0]:
            return False, "Embedding model returned an empty vector"
        return True, f"OK — embedding model returned {len(vecs[0])} dims"

    # ---- vision -----------------------------------------------------------

    async def describe_image(
        self,
        image_path: str | Path,
        *,
        prompt: str = "Describe this image precisely. If it contains text, transcribe it.",
        model: str | None = None,
        max_tokens: int = 400,
    ) -> str:
        model = model or get_settings().vision_model or get_settings().chat_model
        if not model:
            raise LMStudioError("no vision/chat model configured")
        p = Path(image_path)
        if not p.exists():
            raise FileNotFoundError(p)
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        suffix = p.suffix.lower().lstrip(".") or "png"
        data_url = f"data:image/{suffix};base64,{b64}"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        return await self.chat(messages, model=model, max_tokens=max_tokens, temperature=0.1)


@lru_cache(maxsize=1)
def get_client() -> LMStudioClient:
    return LMStudioClient()


def reset_client_cache() -> None:
    get_client.cache_clear()
