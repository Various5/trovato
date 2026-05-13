"""Centralised logging via loguru with rotation in the user data dir."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.config import get_settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    logger.remove()
    logger.add(
        sys.stderr,
        level=s.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - {message}",
        enqueue=True,
    )
    Path(s.logs_path).mkdir(parents=True, exist_ok=True)
    logger.add(
        s.logs_path / "localdoc.log",
        level=s.log_level,
        rotation="10 MB",
        retention=5,
        compression="zip",
        enqueue=True,
        backtrace=False,
        diagnose=s.debug,
    )
    _configured = True


__all__ = ["logger", "setup_logging"]
