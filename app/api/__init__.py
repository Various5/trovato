from fastapi import APIRouter, Depends

from app.api.routes import (
    about,
    auth,
    backup,
    chat,
    diagnostics,
    documents,
    export,
    health,
    lmstudio,
    saved_searches,
    scan,
    search,
    settings,
    sources,
    tags,
    users,
)
from app.auth.security import require_admin

# Routers that manage the install (settings, sources, scans, users, models,
# backups, diagnostics, the global tag taxonomy) are ADMIN-ONLY. The NiceGUI UI
# performs these in-process (not via /api/*), so gating them here is transparent
# for the app while stopping a non-admin user — or a CSRF'd browser — from e.g.
# setting tesseract_cmd (code exec), repointing LM Studio (SSRF), restoring the
# DB, or scanning arbitrary server directories.
_admin = [Depends(require_admin)]

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"], dependencies=_admin)
api_router.include_router(sources.router, prefix="/sources", tags=["sources"], dependencies=_admin)
api_router.include_router(scan.router, prefix="/scan", tags=["scan"], dependencies=_admin)
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(saved_searches.router, prefix="/saved-searches", tags=["search"])
api_router.include_router(chat.router, prefix="/chats", tags=["chat"])
api_router.include_router(tags.router, prefix="/tags", tags=["tags"], dependencies=_admin)
api_router.include_router(backup.router, prefix="/backup", tags=["backup"], dependencies=_admin)
api_router.include_router(settings.router, prefix="/settings", tags=["settings"], dependencies=_admin)
api_router.include_router(lmstudio.router, prefix="/lmstudio", tags=["lmstudio"], dependencies=_admin)
api_router.include_router(export.router, prefix="/export", tags=["export"])
api_router.include_router(
    diagnostics.router, prefix="/diagnostics", tags=["diagnostics"], dependencies=_admin
)
api_router.include_router(about.router, prefix="/about", tags=["about"])
api_router.include_router(health.router, prefix="/health", tags=["health"])
