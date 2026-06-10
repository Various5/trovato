"""About / system info endpoint."""

from __future__ import annotations

import platform
import sys
from typing import Any

from fastapi import APIRouter, Depends

from app import __app_name__, __author__, __contact__, __handle__, __version__
from app.auth.security import login_required
from app.models import User
from app.vectorstore import collection_size

router = APIRouter()


# Auth-gated: previously anonymous, which let a remote attacker trigger the
# outbound update fetch and (via about) read the host data dir + LM Studio URL.
@router.get("/check-update")
async def check_update(_user: User = Depends(login_required)) -> dict[str, Any]:
    from dataclasses import asdict

    from app.services.updates import check_for_update

    return asdict(await check_for_update())


@router.get("")
def about(_user: User = Depends(login_required)) -> dict[str, Any]:
    # Internal paths (data_dir) and the LM Studio URL are intentionally NOT
    # returned — they leaked the host username and internal network layout.
    return {
        "app": __app_name__,
        "version": __version__,
        "author": __author__,
        "contact": __contact__,
        "handle": __handle__,
        "python": sys.version,
        "platform": platform.platform(),
        "chroma_chunks": collection_size(),
        "license": "MIT",
        "privacy": "All processing is local. No cloud calls unless you point a source at a cloud-sync folder.",
        "github": "https://github.com/Various5/localdoc-intelligence",
    }
