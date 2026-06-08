"""Thin async wrapper around the LM Studio ``lms`` CLI.

The OpenAI-compatible REST API can run and list models but cannot *download*
them — only the ``lms`` CLI (or the LM Studio SDK) can. This module locates
``lms`` (on PATH, or the default ``~/.lmstudio/bin`` install location) and shells
out for the two operations the REST surface lacks:

* ``download(model)`` → ``lms get -y <model>`` (can take minutes — it's a
  multi-GB pull), and
* ``load(model)``     → ``lms load -y <model>`` (used as a fallback when JIT
  loading is disabled).

Everything is best-effort and returns ``(ok, output)`` rather than raising.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path

from app.utils.logging import logger

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@lru_cache(maxsize=1)
def lms_path() -> str | None:
    """Locate the ``lms`` executable, or ``None`` if LM Studio's CLI isn't here.

    Checks PATH first, then the default per-user install dir LM Studio creates
    (``~/.lmstudio/bin/lms[.exe]``). Cached — call ``lms_path.cache_clear()`` if
    LM Studio is installed mid-session.
    """
    found = shutil.which("lms")
    if found:
        return found
    exe = "lms.exe" if os.name == "nt" else "lms"
    candidate = Path.home() / ".lmstudio" / "bin" / exe
    if candidate.exists():
        return str(candidate)
    return None


def is_available() -> bool:
    return lms_path() is not None


async def _run(args: list[str], *, timeout: float) -> tuple[bool, str]:
    exe = lms_path()
    if not exe:
        return False, "lms CLI not found — install LM Studio (or run `npx lmstudio install-cli`)"
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:  # pragma: no cover - launch failure is environment-specific
        return False, f"failed to launch lms: {type(e).__name__}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False, f"`lms {' '.join(args)}` timed out after {int(timeout)}s"
    text = _ANSI.sub("", (out or b"").decode("utf-8", "replace")).strip()
    return proc.returncode == 0, text


async def download(model: str, *, timeout: float = 3600.0) -> tuple[bool, str]:
    """``lms get -y <model>`` — resolves and downloads a model. Long-running."""
    logger.info("lms get {}", model)
    ok, out = await _run(["get", "-y", model], timeout=timeout)
    logger.info("lms get {} -> {}", model, "ok" if ok else f"FAILED: {out[:200]}")
    return ok, out


async def load(model: str, *, ttl: int | None = None, timeout: float = 600.0) -> tuple[bool, str]:
    """``lms load -y <model>`` — load a downloaded model into memory.

    With no ``ttl`` the model stays resident until explicitly unloaded (LM Studio
    won't idle-evict it), which is what keeps it hot across chat turns instead of
    reloading every message.
    """
    args = ["load", "-y", model]
    if ttl:
        args += ["--ttl", str(int(ttl))]
    logger.info("lms load {}", model)
    return await _run(args, timeout=timeout)


async def unload_all(*, timeout: float = 60.0) -> tuple[bool, str]:
    """``lms unload --all`` — free every loaded model (used on app shutdown)."""
    logger.info("lms unload --all")
    return await _run(["unload", "--all"], timeout=timeout)
