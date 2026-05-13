"""Text chunking with token-aware sliding window.

Uses tiktoken's ``cl100k_base`` as an approximation; the real model may use a
different tokenizer but the count is close enough for chunking budgets.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover
    _enc = None


def count_tokens(text: str) -> int:
    if _enc is None:
        # rough heuristic: ~4 chars/token
        return max(1, len(text) // 4)
    return len(_enc.encode(text))


@dataclass
class Chunk:
    text: str
    page_from: int
    page_to: int
    token_count: int


def _encode(text: str) -> list[int]:
    if _enc is None:
        # fall back to character-level chunks
        return list(range(len(text)))
    return _enc.encode(text)


def _decode(tokens: list[int], original: str | None = None) -> str:
    if _enc is None and original is not None:
        # naive char-based decode using indices
        return "".join(original[i] for i in tokens if 0 <= i < len(original))
    if _enc is None:
        return ""
    return _enc.decode(tokens)


def chunk_text(
    pages: list[tuple[int, str]],
    *,
    chunk_tokens: int = 1100,
    overlap: int = 150,
) -> Iterator[Chunk]:
    """Chunk a list of ``(page_number, text)`` tuples.

    Page boundaries are respected: chunks track the inclusive page range
    they span so the UI can jump back to the right page.
    """
    if not pages:
        return

    # Encode page-by-page and track page boundaries within the token stream
    page_token_lists: list[tuple[int, list[int]]] = []
    for page_no, text in pages:
        if not text or not text.strip():
            continue
        page_token_lists.append((page_no, _encode(text)))

    if not page_token_lists:
        return

    # Flatten with page markers
    flat: list[int] = []
    page_for_token: list[int] = []
    for page_no, toks in page_token_lists:
        flat.extend(toks)
        page_for_token.extend([page_no] * len(toks))

    n = len(flat)
    if n == 0:
        return
    step = max(1, chunk_tokens - overlap)

    original_text = "\n".join(t for _, t in pages)
    start = 0
    while start < n:
        end = min(n, start + chunk_tokens)
        chunk_tokens_slice = flat[start:end]
        if _enc is None:
            text = original_text[start:end]
        else:
            text = _decode(chunk_tokens_slice)
        if text.strip():
            p_from = page_for_token[start]
            p_to = page_for_token[end - 1]
            yield Chunk(
                text=text.strip(),
                page_from=p_from,
                page_to=p_to,
                token_count=len(chunk_tokens_slice),
            )
        if end == n:
            break
        start += step
