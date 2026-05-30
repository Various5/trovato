"""Lightweight update checker.

Pings the GitHub Releases API (or a user-configured URL) and compares the
published version to the running one. We never auto-overwrite the binary —
the user clicks through to the downloaded installer manually.

Default endpoint: GitHub Releases for this repo. Override by adding
``update_check_url`` to ``settings.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app import __version__
from app.config import get_settings, load_user_settings
from app.utils.logging import logger

DEFAULT_UPDATE_URL = "https://api.github.com/repos/Various5/localdoc-intelligence/releases/latest"
INSTALLER_ASSET_HINTS = ("installer", "setup", ".exe")

# release (padded to 4 parts), pre-release rank, pre-release number
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[-_.]?(a|b|c|rc|alpha|beta)\.?(\d+)?)?", re.IGNORECASE)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}
_FINAL_RANK = 9  # a final release sorts above any pre-release of the same version


def _parse_version(s: str) -> tuple[tuple[int, ...], int, int]:
    """Parse a PEP440-ish version into a comparable key.

    Handles pre-release suffixes so ``0.4.0b1 < 0.4.0`` and
    ``0.4.0b1 < 0.4.0b2`` order correctly — the old parser dropped the suffix
    and treated ``0.4.0b1`` as equal to ``0.4.0``.
    """
    m = _VERSION_RE.match((s or "").strip().lstrip("vV"))
    if not m:
        return ((0, 0, 0, 0), _FINAL_RANK, 0)
    rel = tuple(int(x) for x in m.group(1).split("."))
    rel = (rel + (0, 0, 0, 0))[:4]
    pre = (m.group(2) or "").lower()
    pre_num = int(m.group(3) or 0)
    rank = _PRE_RANK.get(pre, _FINAL_RANK) if pre else _FINAL_RANK
    return (rel, rank, pre_num)


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
        # Degrade gracefully (no banner) but leave a breadcrumb — a silent
        # swallow here is why a broken update URL went unnoticed.
        logger.warning("update check failed ({}): {}", url, e)
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
