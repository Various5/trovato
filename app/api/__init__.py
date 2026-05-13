from fastapi import APIRouter

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
    scan,
    search,
    settings,
    sources,
    tags,
    users,
)


api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(sources.router, prefix="/sources", tags=["sources"])
api_router.include_router(scan.router, prefix="/scan", tags=["scan"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(chat.router, prefix="/chats", tags=["chat"])
api_router.include_router(tags.router, prefix="/tags", tags=["tags"])
api_router.include_router(backup.router, prefix="/backup", tags=["backup"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(lmstudio.router, prefix="/lmstudio", tags=["lmstudio"])
api_router.include_router(export.router, prefix="/export", tags=["export"])
api_router.include_router(diagnostics.router, prefix="/diagnostics", tags=["diagnostics"])
api_router.include_router(about.router, prefix="/about", tags=["about"])
api_router.include_router(health.router, prefix="/health", tags=["health"])
