"""Lightweight update checker.

Pings the GitHub Releases API (or a user-configured URL) and compares the
published version to the running one. We never auto-overwrite the binary —
the user clicks through to the downloaded installer manually.

Default endpoint: GitHub Releases for this repo. Override by adding
``update_check_url`` to ``settings.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app import __version__
from app.config import get_settings, load_user_settings

DEFAULT_UPDATE_URL = "https://api.github.com/repos/Various5/localdoc-intelligence/releases/latest"
INSTALLER_ASSET_HINTS = ("installer", "setup", ".exe")


def _parse_version(s: str) -> tuple[int, ...]:
    parts = s.lstrip("vV").split(".")
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


def _endpoint() -> str:
    return load_user_settings().get("update_check_url") or DEFAULT_UPDATE_URL


def _pick_installer_url(data: dict[str, Any]) -> str | None:
    """Pick a sensible download URL from a GitHub-Releases payload.

    Preference order:
      1. an asset whose name contains "installer" / "setup" / ends in .exe
      2. any .exe asset
      3. the release html_url (so the user lands on the release page)
    """
    assets = data.get("assets") or []
    if assets:
        # First pass — installer-ish names
        for asset in assets:
            name = (asset.get("name") or "").lower()
            if any(h in name for h in INSTALLER_ASSET_HINTS) and name.endswith(".exe"):
                return asset.get("browser_download_url") or asset.get("url")
        # Fallback — first .exe asset
        for asset in assets:
            name = (asset.get("name") or "").lower()
            if name.endswith(".exe"):
                return asset.get("browser_download_url") or asset.get("url")
    return data.get("url") or data.get("html_url")


async def check_for_update(timeout: float = 6.0) -> UpdateInfo:
    url = _endpoint()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": f"LocalDoc-Intelligence/{__version__}"},
        ) as client:
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
            error=f"{type(e).__name__}: {e}",
        )

    latest = data.get("version") or data.get("tag_name") or data.get("name")
    if not latest:
        return UpdateInfo(
            current=__version__,
            latest=None,
            url=None,
            notes=None,
            up_to_date=True,
            error="malformed payload (no version field)",
        )

    up_to_date = _parse_version(latest) <= _parse_version(__version__)
    return UpdateInfo(
        current=__version__,
        latest=latest,
        url=_pick_installer_url(data),
        notes=data.get("notes") or data.get("body"),
        up_to_date=up_to_date,
    )


_ = get_settings  # silence unused-import warning
