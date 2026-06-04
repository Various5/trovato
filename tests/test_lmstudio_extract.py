"""Tests for LM Studio answer extraction (_message_text).

Guards the "model returned no answer" bug: reasoning models stream/return their
output in reasoning_content with an empty content field, so extraction must fall
back to it instead of yielding an empty string.
"""

from __future__ import annotations

from app.llm.lmstudio import _message_text, _normalize_base_url


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
