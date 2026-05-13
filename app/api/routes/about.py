"""About / system info endpoint."""

from __future__ import annotations

import platform
import sys
from typing import Any

from fastapi import APIRouter

from app import __app_name__, __author__, __contact__, __handle__, __version__
from app.config import get_settings
from app.vectorstore import collection_size


router = APIRouter()


@router.get("/check-update")
async def check_update() -> dict[str, Any]:
    from dataclasses import asdict

    from app.services.updates import check_for_update

    return asdict(await check_for_update())


@router.get("")
def about() -> dict[str, Any]:
    s = get_settings()
    return {
        "app": __app_name__,
        "version": __version__,
        "author": __author__,
        "contact": __contact__,
        "handle": __handle__,
        "python": sys.version,
        "platform": platform.platform(),
        "data_dir": str(s.data_path),
        "lmstudio_base_url": s.lmstudio_base_url,
        "chroma_chunks": collection_size(),
        "license": "MIT",
        "privacy": "All processing is local. No cloud calls unless you point a source at a cloud-sync folder.",
        "github": "https://github.com/varous555/localdoc-intelligence",
    }
