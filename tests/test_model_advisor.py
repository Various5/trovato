"""Unit tests for the hardware-aware model advisor (pure, no I/O)."""

from __future__ import annotations

from app.services.model_advisor import classify_role, recommend

# A subset mirroring a real, well-stocked LM Studio install.
_AVAILABLE = [
    {"id": "text-embedding-bge-m3", "type": "embeddings"},
    {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings"},
    {"id": "qwen2.5-vl-32b-instruct", "type": "vlm"},
    {"id": "qwen2.5-vl-72b-instruct", "type": "vlm"},
    {"id": "openai/gpt-oss-20b", "type": "llm"},
    {"id": "openai/gpt-oss-120b", "type": "llm"},
    {"id": "qwen2.5-72b-instruct", "type": "llm"},
    {"id": "deepseek/deepseek-r1-0528-qwen3-8b", "type": "llm"},
]


def test_classify_role_uses_declared_type_then_name() -> None:
    assert classify_role("text-embedding-bge-m3", "embeddings") == "embedding"
    assert classify_role("qwen2.5-vl-7b", "vlm") == "vision"
    assert classify_role("some-mystery-model", "llm") == "chat"
    # No declared type → fall back to name heuristics.
    assert classify_role("nomic-embed-text") == "embedding"
    assert classify_role("llava-llama-3-8b") == "vision"
    assert classify_role("qwen2.5-7b-instruct") == "chat"


def test_balanced_high_prefers_curated_picks() -> None:
    plan = recommend(_AVAILABLE, tier="high", chat_preference="balanced")
    assert plan.embedding.model == "text-embedding-bge-m3"  # quality-first embedding
    assert plan.chat.model == "openai/gpt-oss-20b"  # balanced, not the 72B/120B
    assert plan.vision.model == "qwen2.5-vl-32b-instruct"  # 32B preferred over 72B at 'high'
    assert plan.picks() == {
        "embedding_model": "text-embedding-bge-m3",
        "chat_model": "openai/gpt-oss-20b",
        "vision_model": "qwen2.5-vl-32b-instruct",
    }
    assert plan.missing() == []


def test_max_quality_picks_the_big_model() -> None:
    plan = recommend(_AVAILABLE, tier="high", chat_preference="max")
    assert plan.chat.model == "openai/gpt-oss-120b"


def test_low_tier_caps_chat_size_even_for_max() -> None:
    # Only a 72B and an 8B available; on a low machine 'max' must not pick the 72B.
    avail = [
        {"id": "qwen2.5-72b-instruct", "type": "llm"},
        {"id": "deepseek/deepseek-r1-0528-qwen3-8b", "type": "llm"},
    ]
    plan = recommend(avail, tier="low", chat_preference="max")
    assert plan.chat.model == "deepseek/deepseek-r1-0528-qwen3-8b"


def test_empty_install_suggests_downloads() -> None:
    plan = recommend([], tier="balanced", chat_preference="balanced")
    assert plan.picks() == {}
    missing_roles = {c.role for c in plan.missing()}
    assert missing_roles == {"embedding", "chat", "vision"}
    assert plan.embedding.suggestion == "text-embedding-bge-m3"
    assert plan.chat.suggestion == "openai/gpt-oss-20b"
    assert plan.vision.suggestion == "qwen2.5-vl-7b-instruct"
    # Each suggestion carries a rough size for the ask-first prompt.
    assert all(c.size_gb and c.size_gb > 0 for c in plan.missing())


def test_vlm_can_stand_in_as_chat_when_no_llm() -> None:
    avail = [{"id": "qwen2.5-vl-7b-instruct", "type": "vlm"}]
    plan = recommend(avail, tier="balanced", chat_preference="balanced")
    assert plan.vision.model == "qwen2.5-vl-7b-instruct"
    assert plan.chat.model == "qwen2.5-vl-7b-instruct"  # falls back to the VLM
