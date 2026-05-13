"""Lightweight update checker.

Pings a configurable JSON endpoint (default: GitHub Releases API) and
compares the published version to the running one. Pure status — the actual
download/install step is intentionally manual (we never auto-overwrite the
binary; users must approve).

Endpoint schema:
    {"version": "0.2.0", "url": "https://...installer.exe", "notes": "..."}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import httpx

from app import __version__
from app.config import get_settings, load_user_settings


def _parse_version(s: str) -> tuple[int, ...]:
    parts = s.lstrip("v").split(".")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p.split("-")[0]))
        except ValueError:
            out.append(0)
    return tuple(out)


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    url: str | None
    notes: str | None
    up_to_date: bool
    error: str | None = None


def _endpoint() -> Optional[str]:
    return load_user_settings().get("update_check_url") or None


async def check_for_update(timeout: float = 6.0) -> UpdateInfo:
    url = _endpoint()
    if not url:
        return UpdateInfo(
            current=__version__,
            latest=None,
            url=None,
            notes=None,
            up_to_date=True,
            error="no update_check_url configured",
        )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return UpdateInfo(
            current=__version__,
            latest=None,
            url=None,
            notes=None,
            up_to_date=True,
            error=str(e),
        )
    latest = data.get("version") or data.get("tag_name")
    if not latest:
        return UpdateInfo(
            current=__version__,
            latest=None,
            url=None,
            notes=None,
            up_to_date=True,
            error="malformed payload",
        )
    up_to_date = _parse_version(latest) <= _parse_version(__version__)
    return UpdateInfo(
        current=__version__,
        latest=latest,
        url=data.get("url") or data.get("html_url"),
        notes=data.get("notes") or data.get("body"),
        up_to_date=up_to_date,
    )


_ = get_settings  # silence unused-import warning
