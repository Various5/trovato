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


def _message_text(msg: dict[str, Any]) -> str:
    """Pull the answer out of a chat ``message``/``delta``.

    Reasoning models (Qwen3, DeepSeek-R1, gpt-oss, …) put their output in
    ``reasoning_content`` (or ``reasoning``) and may leave ``content`` empty, so
    fall back to those — otherwise the answer looks blank even though the model
    replied.
    """
    if not isinstance(msg, dict):
        return ""
    return msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""


def _normalize_base_url(base: str) -> str:
    """Ensure the URL targets LM Studio's OpenAI-compatible ``/v1`` API.

    People routinely enter a bare ``host:port`` (e.g. ``http://10.0.1.40:1234``).
    LM Studio serves the OpenAI API under ``/v1``; a bare ``/chat/completions``
    then hits its "Unexpected endpoint or method … Returning 200 anyway" handler,
    so the app gets a 200 with no ``choices`` and chat looks empty. If the URL
    has a scheme + host but no path, append ``/v1``.
    """
    from urllib.parse import urlparse

    base = (base or "").rstrip("/")
    try:
        p = urlparse(base)
    except Exception:
        return base
    if p.scheme and p.netloc and not p.path.strip("/"):
        return base + "/v1"
    return base


# Cache the probed context length briefly so we don't hit /api/v0/models on
# every chat turn, but still pick up a model reload within a couple of minutes.
_CTX_CACHE: dict[tuple[str, str], tuple[float, int | None]] = {}
_CTX_TTL = 120.0


def context_char_budget(ctx_tokens: int | None, *, output_tokens: int = 900) -> int:
    """Characters of prompt *input* that safely fit a model's context window.

    Reserves room for the response and the system/history scaffolding, and is
    deliberately conservative (≈3.2 chars/token) so we under-fill rather than
    trip LM Studio's "nkeep >= nctx" rejection. Falls back to an 8k-token
    assumption when the server won't report a context length.
    """
    if not ctx_tokens or ctx_tokens <= 0:
        ctx_tokens = 8192
    usable = max(ctx_tokens - output_tokens - 800, 400)  # 800: system + chat overhead + margin
    return max(1200, min(int(usable * 3.2), 32000))


class LMStudioClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        s = get_settings()
        self.base_url = _normalize_base_url(base_url or s.lmstudio_base_url)
        self.api_key = api_key or s.lmstudio_api_key or "lm-studio"
        if timeout is None:
            # Slower (CPU-only) backends get a longer grace; fast machines
            # fail quicker. Resolved from the active performance profile.
            from app.services.hardware import active_tuning

            timeout = active_tuning().http_timeout
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

    async def model_context_length(self, model: str | None = None) -> int | None:
        """Loaded context window (tokens) of the chat model, read from LM Studio's
        native ``/api/v0/models``. Returns ``None`` if the server doesn't report
        it. Cached briefly so it isn't probed on every chat turn."""
        import time as _time

        model = (model or get_settings().chat_model or "").strip()
        key = (self.base_url, model)
        now = _time.time()
        cached = _CTX_CACHE.get(key)
        if cached and now - cached[0] < _CTX_TTL:
            return cached[1]
        ctx: int | None = None
        try:
            async with await self._client() as c:
                base = str(c.base_url).rstrip("/")
                native = (base[: -len("/v1")] if base.endswith("/v1") else base) + "/api/v0/models"
                r = await c.get(native)
                if r.status_code == 200:
                    data = r.json()
                    items = (
                        (data.get("data") or data.get("models") or [])
                        if isinstance(data, dict)
                        else (data or [])
                    )
                    chosen = None
                    for it in items:  # prefer the exact configured model
                        if isinstance(it, dict) and (it.get("id") or it.get("model_key")) == model:
                            chosen = it
                            break
                    if chosen is None:  # else any loaded LLM
                        for it in items:
                            if (
                                isinstance(it, dict)
                                and it.get("state") == "loaded"
                                and it.get("type") in ("llm", "vlm", None)
                            ):
                                chosen = it
                                break
                    if chosen:
                        raw = chosen.get("loaded_context_length") or chosen.get("max_context_length")
                        ctx = int(raw) if raw else None
        except Exception as e:
            logger.debug("model_context_length probe failed: {}", e)
        _CTX_CACHE[key] = (now, ctx)
        return ctx

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

    # ---- loading / warmup -------------------------------------------------

    async def loaded_model_ids(self) -> set[str]:
        """Ids of models LM Studio currently has *loaded* (state == 'loaded'),
        read from the native ``/api/v0/models``. Empty set on any failure."""
        out: set[str] = set()
        try:
            async with await self._client() as c:
                base = str(c.base_url).rstrip("/")
                native = (base[: -len("/v1")] if base.endswith("/v1") else base) + "/api/v0/models"
                r = await c.get(native)
                if r.status_code == 200:
                    data = r.json()
                    items = (
                        (data.get("data") or data.get("models") or [])
                        if isinstance(data, dict)
                        else (data or [])
                    )
                    for it in items:
                        if isinstance(it, dict) and it.get("state") == "loaded":
                            mid = it.get("id") or it.get("model_key")
                            if mid:
                                out.add(mid)
        except Exception as e:
            logger.debug("loaded_model_ids probe failed: {}", e)
        return out

    async def ensure_loaded(self, model: str | None = None, *, kind: str = "chat") -> tuple[bool, str]:
        """Make sure ``model`` is loaded in LM Studio *before* real work hits it.

        Strategy: if it already reports ``loaded``, done. Otherwise fire a
        minimal request (an embed for embeddings, a 1-token chat for chat/vision)
        which triggers LM Studio's just-in-time load. If JIT loading is disabled
        the request fails, so we fall back to ``lms load`` when the CLI is around.
        Returns ``(ok, message)`` and never raises.
        """
        model = (model or "").strip()
        if not model:
            return False, "no model configured"
        try:
            if model in await self.loaded_model_ids():
                return True, f"{model} already loaded"
        except Exception:
            pass
        try:
            if kind == "embedding":
                await self.embed(["warm-up"], model=model)
            else:
                await self.chat(
                    [{"role": "user", "content": "."}], model=model, max_tokens=1, temperature=0.0
                )
            return True, f"{model} loaded"
        except Exception as e:
            from app.services import lms_cli

            if lms_cli.is_available():
                ok, out = await lms_cli.load(model)
                return ok, (f"{model} loaded via lms" if ok else f"lms load failed: {out[:200]}")
            return False, f"could not load {model}: {type(e).__name__}: {e}"

    async def list_downloaded(self) -> list[dict[str, Any]]:
        """All *downloaded* models from LM Studio's native ``/api/v0/models``,
        including ``type`` (llm/vlm/embeddings), ``state`` and context length —
        the metadata the model advisor needs. Falls back to ``list_models`` (ids
        only, no type) if the native endpoint can't be reached."""
        try:
            async with await self._client() as c:
                base = str(c.base_url).rstrip("/")
                native = (base[: -len("/v1")] if base.endswith("/v1") else base) + "/api/v0/models"
                r = await c.get(native)
                if r.status_code == 200:
                    data = r.json()
                    items = (
                        (data.get("data") or data.get("models") or [])
                        if isinstance(data, dict)
                        else (data or [])
                    )
                    out = [it for it in items if isinstance(it, dict)]
                    if out:
                        return out
        except Exception as e:
            logger.debug("list_downloaded failed: {}", e)
        return await self.list_models()

    # ---- chat -------------------------------------------------------------

    # Retry only transient transport errors (connect/timeout) — a malformed
    # response or a config problem won't fix itself on retry, and wrapping it in
    # a RetryError just hides the real cause.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=0.5, max=4),
        retry=retry_if_exception_type(httpx.TransportError),
    )
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
            try:
                data = r.json()
            except Exception:
                raise LMStudioError(
                    "LM Studio returned a non-JSON response — check the Base URL points at "
                    f"the OpenAI API (…/v1). Got: {r.text[:200]!r}"
                )
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                # The classic 'Unexpected endpoint … Returning 200 anyway' body,
                # or no model loaded — surface it instead of a blank answer.
                raise LMStudioError(
                    "LM Studio returned no completion. Make sure a chat model is loaded "
                    "and the Base URL ends in /v1 (Settings → LM Studio). "
                    f"Response: {str(data)[:200]}"
                )
            return _message_text(choices[0].get("message") or {})

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
                import json as _json

                content_seen = False
                reasoning_buf: list[str] = []
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    chunk = line[6:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        choices = _json.loads(chunk).get("choices") or []
                        if not choices:
                            continue  # role-only / usage-only / keep-alive chunk
                        delta = choices[0].get("delta") or {}
                    except Exception as e:
                        logger.debug("chat_stream: skipped unparseable chunk {!r}: {}", chunk[:120], e)
                        continue
                    content = delta.get("content")
                    if content:
                        content_seen = True
                        yield content
                    else:
                        r_piece = delta.get("reasoning_content") or delta.get("reasoning")
                        if r_piece:
                            reasoning_buf.append(r_piece)
                # Reasoning models stream their chain-of-thought in reasoning_content
                # before the answer. Only surface it if the model never produced real
                # answer content — otherwise normal replies stay clean and the hidden
                # thinking isn't dumped into (and saved with) the answer.
                if not content_seen and reasoning_buf:
                    yield "".join(reasoning_buf)

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
        from urllib.parse import urlsplit

        base = self.base_url.rstrip("/")
        parts = urlsplit(base)
        host_root = f"{parts.scheme}://{parts.netloc}"
        # Build *absolute* URLs and POST them through a single fresh client.
        # Previously we mixed relative paths (resolved against base_url) with
        # an absolute /api/v0 URL; httpx silently rewrites the absolute URL
        # back against base_url under some build conditions, so the native
        # endpoint never got hit. Absolute URLs everywhere = no surprises.
        attempts: list[tuple[str, str]] = [
            ("native", f"{host_root}/api/v0/embeddings"),
            ("openai-compat", f"{base}/embeddings"),
            ("singular", f"{base}/embedding"),
        ]
        payload = {"model": model, "input": items}
        attempt_log: list[str] = []
        data: Any = None

        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers) as ac:
            for kind, url in attempts:
                try:
                    r = await ac.post(url, json=payload)
                except Exception as e:
                    attempt_log.append(f"{kind} {url}: {type(e).__name__}: {e}")
                    continue
                if r.status_code == 404:
                    attempt_log.append(f"{kind} {url}: 404")
                    continue
                if r.status_code >= 400:
                    raise LMStudioError(f"embeddings failed at {url}: {r.status_code} {r.text[:300]}")
                try:
                    candidate = r.json()
                except Exception as e:
                    attempt_log.append(f"{kind} {url}: invalid JSON: {e}")
                    continue
                if (
                    isinstance(candidate, dict)
                    and "error" in candidate
                    and not candidate.get("data")
                    and not candidate.get("embeddings")
                ):
                    attempt_log.append(f"{kind} {url}: 200/error {str(candidate.get('error'))[:80]}")
                    continue
                attempt_log.append(f"{kind} {url}: 200 ✓")
                data = candidate
                break

        if data is None:
            raise LMStudioError(
                "no embeddings endpoint responded with vectors.\n  " + "\n  ".join(attempt_log)
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


async def warm_up_configured() -> dict[str, tuple[bool, str]]:
    """Preload the configured chat / embedding / vision models into LM Studio so
    they're hot before the first scan or chat. Best-effort: skips unset models,
    skips already-loaded ones, and never raises. Returns ``{role: (ok, msg)}``."""
    s = get_settings()
    client = get_client()
    try:
        loaded = await client.loaded_model_ids()
    except Exception:
        loaded = set()
    results: dict[str, tuple[bool, str]] = {}
    for kind, model in (
        ("embedding", s.embedding_model),
        ("chat", s.chat_model),
        ("vision", s.vision_model),
    ):
        m = (model or "").strip()
        if not m:
            continue
        if m in loaded:
            results[kind] = (True, "already loaded")
            continue
        try:
            results[kind] = await client.ensure_loaded(m, kind=kind)
        except Exception as e:  # pragma: no cover - defensive
            results[kind] = (False, f"{type(e).__name__}: {e}")
        logger.info("warm-up {} ({}): {}", kind, m, results[kind][1])
    return results
