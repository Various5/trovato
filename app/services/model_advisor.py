"""Hardware-aware model recommendation.

Given the models a user already has in LM Studio (from ``/api/v0/models``, which
reports a ``type`` of ``llm`` / ``vlm`` / ``embeddings``) plus the detected
hardware tier, pick a sensible default for each role:

* **embedding** — quality first (bge-m3), small/fast fallback (nomic).
* **vision**   — a VLM sized to the tier (Qwen2.5-VL family preferred).
* **chat**     — leans on the user's ``chat_preference`` (fastest / balanced /
  max quality), capped so a weak machine never gets steered onto a 70B.

The picker only ever *chooses from already-downloaded models* (the "ask-first"
download policy). When a role has no suitable local model it records a
``suggestion`` (an ``lms get`` target + rough size) so the caller can prompt
before pulling several GB.

This module is pure (no I/O) so it can be unit-tested with a fixed model list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- curated preference lists (matched as case-insensitive substrings) -------

_EMBED_PREF = (
    "bge-m3",
    "bge-large",
    "nomic-embed-text-v1.5",
    "nomic-embed",
    "e5-large",
    "gte-large",
    "snowflake-arctic-embed",
    "mxbai-embed",
)

_VISION_PREF = {
    "high": (
        "qwen2.5-vl-32b",
        "qwen2.5-vl-7b",
        "qwen2-vl-7b",
        "llava-llama-3-8b",
        "minicpm-v",
        "qwen2.5-vl-72b",
        "moondream",
    ),
    "balanced": (
        "qwen2.5-vl-7b",
        "qwen2-vl-7b",
        "minicpm-v",
        "llava-llama-3-8b",
        "qwen2.5-vl-32b",
        "moondream",
    ),
    "low": (
        "moondream",
        "qwen2.5-vl-3b",
        "qwen2.5-vl-7b",
        "llava-llama-3-8b",
    ),
}

_CHAT_PREF = {
    "fastest": (
        "qwen2.5-7b-instruct",
        "llama-3.1-8b",
        "deepseek-r1-0528-qwen3-8b",
        "phi-4",
        "gemma-2-9b",
        "gpt-oss-20b",
    ),
    "balanced": (
        "gpt-oss-20b",
        "qwen2.5-14b",
        "glm-4.7-flash",
        "phi-4",
        "gemma-2-27b",
        "qwen2.5-32b",
        "deepseek-r1-0528-qwen3-8b",
    ),
    "max": (
        "gpt-oss-120b",
        "qwen2.5-72b",
        "minimax-m2.5",
        "llama-3.3-70b",
        "qwen2.5-32b",
        "gpt-oss-20b",
    ),
}

# Approximate parameter ceiling (in billions) per hardware tier so "max quality"
# on a 4 GB laptop doesn't try to pick a 70B. ``None`` == no cap.
_TIER_PARAM_CAP = {"low": 9.0, "balanced": 34.0, "high": None}

# What to offer downloading when a role has nothing suitable locally.
# (id is an ``lms get`` search target; size_gb is a rough download size.)
_DOWNLOAD = {
    "embedding": {
        "low": ("text-embedding-nomic-embed-text-v1.5", 0.1),
        "balanced": ("text-embedding-bge-m3", 0.6),
        "high": ("text-embedding-bge-m3", 0.6),
    },
    "vision": {
        "low": ("moondream2", 1.8),
        "balanced": ("qwen2.5-vl-7b-instruct", 6.0),
        "high": ("qwen2.5-vl-7b-instruct", 6.0),
    },
    "chat": {
        "fastest": ("qwen2.5-7b-instruct", 5.0),
        "balanced": ("openai/gpt-oss-20b", 12.0),
        "max": ("openai/gpt-oss-120b", 63.0),
    },
}

_EMBED_KEYS = ("embed", "bge", "nomic", "e5-", "gte-", "snowflake-arctic", "mxbai")
_VISION_KEYS = ("-vl", "vision", "llava", "moondream", "internvl", "minicpm-v")


@dataclass
class RoleChoice:
    role: str  # 'embedding' | 'chat' | 'vision'
    model: str | None = None  # chosen id from the downloaded set, else None
    suggestion: str | None = None  # lms get target when model is None
    size_gb: float | None = None  # rough download size for the suggestion
    note: str = ""

    @property
    def missing(self) -> bool:
        return self.model is None


@dataclass
class ModelPlan:
    tier: str
    chat_preference: str
    embedding: RoleChoice = field(default_factory=lambda: RoleChoice("embedding"))
    chat: RoleChoice = field(default_factory=lambda: RoleChoice("chat"))
    vision: RoleChoice = field(default_factory=lambda: RoleChoice("vision"))

    @property
    def choices(self) -> list[RoleChoice]:
        return [self.embedding, self.chat, self.vision]

    def picks(self) -> dict[str, str]:
        """``{settings_key: model_id}`` for roles that resolved to a local model."""
        out: dict[str, str] = {}
        if self.embedding.model:
            out["embedding_model"] = self.embedding.model
        if self.chat.model:
            out["chat_model"] = self.chat.model
        if self.vision.model:
            out["vision_model"] = self.vision.model
        return out

    def missing(self) -> list[RoleChoice]:
        return [c for c in self.choices if c.missing]


def _param_b(model_id: str) -> float | None:
    """Best-effort parameter count (billions) parsed from a model id, e.g.
    ``qwen2.5-14b`` → 14.0. Returns ``None`` when no ``<n>b`` token is present."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_id.lower())
    return float(m.group(1)) if m else None


def classify_role(model_id: str, declared_type: str | None = None) -> str:
    """Bucket a model into embedding / vision / chat.

    Prefers LM Studio's declared ``type`` (llm/vlm/embeddings) and falls back to
    name conventions when the type is absent (e.g. the OpenAI ``/v1/models``
    surface, which doesn't report it).
    """
    dt = (declared_type or "").lower()
    if dt == "embeddings":
        return "embedding"
    if dt == "vlm":
        return "vision"
    lid = model_id.lower()
    if any(k in lid for k in _EMBED_KEYS):
        return "embedding"
    if any(k in lid for k in _VISION_KEYS):
        return "vision"
    return "chat"


def _normalize(available: list[dict]) -> dict[str, list[str]]:
    """Group available model ids by role, preserving input order."""
    buckets: dict[str, list[str]] = {"embedding": [], "chat": [], "vision": []}
    for m in available:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("model") or m.get("model_key")
        if not mid:
            continue
        role = classify_role(mid, m.get("type"))
        buckets[role].append(mid)
        # A VLM can also stand in as a chat model if nothing better turns up.
        if role == "vision":
            buckets["chat"].append(mid)
    return buckets


def _pick(candidates: list[str], preference: tuple[str, ...], cap_b: float | None) -> str | None:
    """Pick the first candidate matching the preference order (respecting the
    param cap); fall back to the smallest under-cap candidate, then any."""
    under_cap = [
        c for c in candidates if cap_b is None or (_param_b(c) is None or _param_b(c) <= cap_b)
    ]
    for want in preference:
        for c in under_cap:
            if want in c.lower():
                return c
    if under_cap:
        # No curated match — smallest known size wins (params asc, unknown last).
        return min(under_cap, key=lambda c: (_param_b(c) is None, _param_b(c) or 0.0))
    # Nothing fits under the cap — still better to return the smallest available
    # than nothing at all.
    return min(candidates, key=lambda c: (_param_b(c) is None, _param_b(c) or 0.0)) if candidates else None


def recommend(
    available: list[dict],
    *,
    tier: str = "balanced",
    chat_preference: str = "balanced",
) -> ModelPlan:
    """Recommend a model for each role from the *downloaded* model list."""
    tier = tier if tier in _TIER_PARAM_CAP else "balanced"
    chat_preference = chat_preference if chat_preference in _CHAT_PREF else "balanced"
    buckets = _normalize(available)
    plan = ModelPlan(tier=tier, chat_preference=chat_preference)

    plan.embedding.model = _pick(buckets["embedding"], _EMBED_PREF, None)
    plan.vision.model = _pick(buckets["vision"], _VISION_PREF[tier], None)
    plan.chat.model = _pick(buckets["chat"], _CHAT_PREF[chat_preference], _TIER_PARAM_CAP[tier])

    # Fill download suggestions for whatever stayed unresolved.
    if plan.embedding.missing:
        sug, sz = _DOWNLOAD["embedding"][tier]
        plan.embedding.suggestion, plan.embedding.size_gb = sug, sz
    if plan.vision.missing:
        sug, sz = _DOWNLOAD["vision"][tier]
        plan.vision.suggestion, plan.vision.size_gb = sug, sz
    if plan.chat.missing:
        sug, sz = _DOWNLOAD["chat"][chat_preference]
        plan.chat.suggestion, plan.chat.size_gb = sug, sz

    return plan
