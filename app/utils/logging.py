"""Centralised logging via loguru with rotation in the user data dir."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.config import get_settings

_configured = False


def setup_logging() -> None:
    """Configure loguru sinks. Idempotent; safe to call from multiple entry
    points."""
    global _configured
    if _configured:
        return
    s = get_settings()
    logger.remove()

    frozen = getattr(sys, "frozen", False)
    # ``enqueue=True`` requires a working multiprocessing primitive. PyInstaller
    # frozen apps can hit edge cases with that on Windows — most prominently
    # when sys.stdin is detached. Default to off in frozen mode; the in-process
    # synchronous sinks are plenty fast for this app's log volume.
    use_queue = not frozen

    if sys.stderr is not None:
        try:
            logger.add(
                sys.stderr,
                level=s.log_level,
                format=(
                    "<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
                    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan>"
                    " - {message}"
                ),
                enqueue=use_queue,
            )
        except Exception:
            # Better to lose console logging than to brick the boot.
            pass

    # File sink. Ensure the directory exists; if it doesn't and we can't create
    # it, drop the sink rather than crashing the app at startup.
    try:
        Path(s.logs_path).mkdir(parents=True, exist_ok=True)
        logger.add(
            s.logs_path / "localdoc.log",
            level=s.log_level,
            rotation="10 MB",
            retention=5,
            compression="zip",
            enqueue=use_queue,
            backtrace=False,
            diagnose=s.debug,
        )
    except Exception as e:  # pragma: no cover — defensive
        try:
            print(f"[localdoc] file log disabled: {e}", file=sys.__stderr__ or sys.stderr)
        except Exception:
            pass

    _configured = True


__all__ = ["logger", "setup_logging"]
