"""Application entry point.

Spins up FastAPI + NiceGUI under one uvicorn process.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import __app_name__, __version__
from app.api import api_router
from app.config import get_settings
from app.database import init_db
from app.ui import register_ui
from app.utils.logging import logger, setup_logging


def create_app() -> FastAPI:
    setup_logging()
    s = get_settings()
    init_db()

    app = FastAPI(
        title=__app_name__,
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        same_site="lax",
        https_only=False,
        max_age=14 * 24 * 3600,
    )

    cors_origins = ["http://localhost", "http://127.0.0.1"]
    if s.allow_lan:
        cors_origins.append("*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.on_event("startup")
    async def _start_watchers() -> None:
        from app.services.indexer import recover_unfinished_jobs
        from app.services.watcher import start_all_active

        try:
            await recover_unfinished_jobs()
        except Exception as e:
            logger.warning("job recovery skipped: {}", e)
        try:
            await start_all_active()
        except Exception as e:
            logger.warning("watcher startup skipped: {}", e)

    # NiceGUI pages live at the root path; this must come AFTER the API include.
    register_ui(app)

    logger.info("{} v{} ready — listening on http://{}:{}", __app_name__, __version__, s.host, s.port)
    return app


def run() -> None:
    s = get_settings()
    host = s.host if not s.allow_lan else "0.0.0.0"
    uvicorn.run(
        "app.main:create_app",
        host=host,
        port=s.port,
        factory=True,
        reload=False,
        log_level=s.log_level.lower(),
    )


# Allow `python -m app.main`
if __name__ == "__main__":
    run()
