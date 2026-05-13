"""Path helpers — normalisation, safe relpath, sanitisation."""

from __future__ import annotations

import os
import re
from pathlib import Path


_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')


def normalize(p: str | Path) -> str:
    return str(Path(os.path.normpath(str(p))).resolve())


def safe_filename(name: str, max_len: int = 180) -> str:
    cleaned = _INVALID.sub("_", name).strip().strip(".") or "file"
    return cleaned[:max_len]


def is_under(child: str | Path, parent: str | Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False
