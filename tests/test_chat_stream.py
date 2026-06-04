"""Tests for LMStudioClient.chat_stream SSE parsing.

Covers the reasoning-model handling: stream clean `content` as it arrives, but
only surface buffered `reasoning_content` if the model never produced real
content (so a model that emits BOTH doesn't leak its chain-of-thought into the
visible answer) — and tolerate choices-less keep-alive/usage chunks.
"""

from __future__ import annotations

import json

import pytest

from app.llm.lmstudio import LMStudioClient


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self) -> bytes:
        return b""


class _FakeClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    def stream(self, *_a, **_k) -> _FakeResp:
        return _FakeResp(self._lines)


def _sse(*deltas: dict) -> list[str]:
    lines = []
    for d in deltas:
        lines.append("data: " + json.dumps({"choices": [{"delta": d}]}))
    lines.append("data: [DONE]")
    return lines


async def _collect(lines: list[str], monkeypatch) -> list[str]:
    client = LMStudioClient(base_url="http://localhost:1234/v1")

    async def _fake():
        return _FakeClient(lines)

    monkeypatch.setattr(client, "_client", _fake)
    return [p async for p in client.chat_stream([{"role": "user", "content": "x"}], model="m")]


async def test_streams_plain_content(monkeypatch) -> None:
    out = await _collect(_sse({"content": "Hel"}, {"content": "lo"}), monkeypatch)
    assert "".join(out) == "Hello"


async def test_reasoning_only_is_surfaced(monkeypatch) -> None:
    # Model emits only reasoning_content (the "no answer" bug) → surface it.
    out = await _collect(_sse({"reasoning_content": "think "}, {"reasoning_content": "harder"}), monkeypatch)
    assert "".join(out) == "think harder"


async def test_reasoning_not_leaked_when_content_present(monkeypatch) -> None:
    # Reasoning first, then real content → only the content is yielded.
    out = await _collect(
        _sse(
            {"reasoning_content": "let me think..."},
            {"content": "The "},
            {"content": "answer."},
        ),
        monkeypatch,
    )
    assert "".join(out) == "The answer."
    assert "think" not in "".join(out)


async def test_tolerates_choiceless_chunks(monkeypatch) -> None:
    lines = [
        "data: " + json.dumps({"choices": []}),  # usage/keep-alive
        "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
        "data: [DONE]",
    ]
    out = await _collect(lines, monkeypatch)
    assert "".join(out) == "ok"


@pytest.mark.parametrize("bad", ["data: not-json", "", "ignored line"])
async def test_skips_garbage_lines(bad: str, monkeypatch) -> None:
    lines = [bad, "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}), "data: [DONE]"]
    out = await _collect(lines, monkeypatch)
    assert "".join(out) == "hi"
