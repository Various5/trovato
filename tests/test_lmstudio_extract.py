"""Tests for LM Studio answer extraction (_message_text).

Guards the "model returned no answer" bug: reasoning models stream/return their
output in reasoning_content with an empty content field, so extraction must fall
back to it instead of yielding an empty string.
"""

from __future__ import annotations

from app.llm.lmstudio import _message_text


def test_prefers_content() -> None:
    assert _message_text({"content": "hello", "reasoning_content": "think"}) == "hello"


def test_falls_back_to_reasoning_content() -> None:
    assert _message_text({"content": "", "reasoning_content": "the answer"}) == "the answer"
    assert _message_text({"content": None, "reasoning": "r"}) == "r"


def test_empty_and_non_dict() -> None:
    assert _message_text({}) == ""
    assert _message_text({"content": None, "reasoning_content": None}) == ""
    assert _message_text(None) == ""  # type: ignore[arg-type]
