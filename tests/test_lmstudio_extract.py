"""Tests for LM Studio answer extraction (_message_text).

Guards the "model returned no answer" bug: reasoning models stream/return their
output in reasoning_content with an empty content field, so extraction must fall
back to it instead of yielding an empty string.
"""

from __future__ import annotations

from app.llm.lmstudio import _message_text, _normalize_base_url, context_char_budget


def test_prefers_content() -> None:
    assert _message_text({"content": "hello", "reasoning_content": "think"}) == "hello"


def test_falls_back_to_reasoning_content() -> None:
    assert _message_text({"content": "", "reasoning_content": "the answer"}) == "the answer"
    assert _message_text({"content": None, "reasoning": "r"}) == "r"


def test_empty_and_non_dict() -> None:
    assert _message_text({}) == ""
    assert _message_text({"content": None, "reasoning_content": None}) == ""
    assert _message_text(None) == ""  # type: ignore[arg-type]


def test_normalize_base_url_appends_v1_to_bare_host() -> None:
    # The reported bug: a bare host (no /v1) makes /chat/completions 404-ish.
    assert _normalize_base_url("http://10.0.1.40:1234") == "http://10.0.1.40:1234/v1"
    assert _normalize_base_url("http://localhost:1234/") == "http://localhost:1234/v1"
    assert _normalize_base_url("https://host:8080") == "https://host:8080/v1"


def test_normalize_base_url_leaves_explicit_paths() -> None:
    assert _normalize_base_url("http://localhost:1234/v1") == "http://localhost:1234/v1"
    assert _normalize_base_url("http://localhost:1234/v1/") == "http://localhost:1234/v1"
    # A custom proxy path is respected, not clobbered.
    assert _normalize_base_url("http://proxy.local/llm/api") == "http://proxy.local/llm/api"


def test_context_char_budget_scales_and_clamps() -> None:
    # Unknown context → safe 8k-token assumption (not the tiny floor).
    assert context_char_budget(None) == context_char_budget(8192)
    # Bigger context → bigger budget; smaller → smaller.
    assert context_char_budget(4096) < context_char_budget(16384)
    # Floor and ceiling. The ceiling is generous so large-context models can
    # absorb many documents for library-wide answers.
    assert context_char_budget(512) >= 1200
    assert context_char_budget(1_000_000) <= 80000
    assert context_char_budget(1_000_000) > 32000
    # A 4k-context model must budget well under its window once output (900) +
    # overhead are reserved — i.e. far below the old fixed 12k-char-per-doc.
    b4k = context_char_budget(4096, output_tokens=900)
    assert b4k < 9000  # ~2.8k tokens of input, leaving room for output + scaffold
