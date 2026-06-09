"""Application entry point.

Boots FastAPI + NiceGUI under a single uvicorn process. Optimised for two
deployment modes:

1. ``python -m app.main`` — local development.
2. PyInstaller ``--noconsole`` Windows bundle — desktop deployment.

The PyInstaller mode is the tricky one: ``sys.stdout`` is ``None``, the
working directory is the launcher dir (not the bundle dir), and any
uncaught exception silently kills the process with no log. The defences
below make that mode survivable and *diagnosable* even if it does crash.
"""

from __future__ import annotations

import datetime as _dt
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Stage 1 — survive the boot before any third-party import runs
# ---------------------------------------------------------------------------


def _crash_dump_dir() -> Path:
    """Best-effort path for a crash-dump file. Falls back through several
    locations so we always get *something* on disk, even on locked-down
    enterprise machines."""
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "LocalDocIntelligence")
    candidates.append(Path.home() / ".localdoc-intelligence")
    candidates.append(Path(os.environ.get("TEMP", "")) / "LocalDocIntelligence")
    candidates.append(Path.cwd())
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            # Test writability
            test = c / ".write-test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
            return c
        except OSError:
            continue
    return Path.cwd()  # last resort


def _write_crash_dump(exc: BaseException, stage: str) -> Path | None:
    """Write a self-contained crash dump. NEVER raises."""
    try:
        dump_dir = _crash_dump_dir()
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = dump_dir / f"crash-{ts}.log"
        with path.open("w", encoding="utf-8", errors="replace") as f:
            f.write("LocalDoc Intelligence — crash dump\n")
            f.write("=" * 60 + "\n")
            f.write(f"stage:       {stage}\n")
            f.write(f"timestamp:   {_dt.datetime.now().isoformat()}\n")
            f.write(f"python:      {sys.version}\n")
            f.write(f"executable:  {sys.executable}\n")
            f.write(f"frozen:      {getattr(sys, 'frozen', False)}\n")
            f.write(f"cwd:         {os.getcwd()}\n")
            f.write(f"argv:        {sys.argv}\n")
            f.write(
                "env (subset): "
                + repr(
                    {k: os.environ.get(k) for k in ("APPDATA", "USERPROFILE", "TEMP", "PYTHONPATH", "PATH")}
                )
                + "\n"
            )
            try:
                from app import __version__

                f.write(f"version:     {__version__}\n")
            except Exception:
                f.write("version:     (could not import app)\n")
            f.write("=" * 60 + "\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        return path
    except Exception:
        return None


def _ensure_console_streams() -> None:
    """PyInstaller ``--noconsole`` leaves ``sys.stdout`` / ``sys.stderr`` as
    ``None``, which makes uvicorn's logger crash. Wire null sinks in.

    The file handles intentionally live for the lifetime of the process —
    they replace the global stdio streams, so a context manager would close
    them too early.
    """
    devnull_path = os.devnull
    if sys.stdout is None:
        sys.stdout = open(devnull_path, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(devnull_path, "w", encoding="utf-8")  # noqa: SIM115


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


# ---------------------------------------------------------------------------
# Stage 2 — actual app construction (deferred imports so a missing dep at the
# top of the file doesn't prevent the crash-dump path from running)
# ---------------------------------------------------------------------------


def create_app():  # type: ignore[no-untyped-def]
    """Build the FastAPI + NiceGUI app. Imported lazily so the crash-dump
    layer above survives even if these imports fail."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.sessions import SessionMiddleware

    from app import __app_name__, __version__
    from app.api import api_router
    from app.config import get_settings
    from app.database import init_db
    from app.ui import register_ui
    from app.utils.logging import logger, setup_logging

    setup_logging()
    s = get_settings()
    s.ensure_dirs()
    init_db()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Background workers — both are best-effort.
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
        # One-time, idempotent: relabel image-description chunks indexed before
        # they carried ChunkSource.image_description, so older libraries answer
        # image questions without a full vision rescan. Off-thread, best-effort.
        try:
            import asyncio as _aio

            from app.services.indexer import backfill_image_chunk_sources

            _aio.create_task(_aio.to_thread(backfill_image_chunk_sources))
        except Exception as e:
            logger.debug("image-chunk backfill not scheduled: {}", e)
        # Preload the configured models into LM Studio so they're hot before the
        # first scan/chat. Detached + best-effort: never blocks boot, and if LM
        # Studio is down it just logs and moves on.
        if getattr(s, "preload_models", True):
            import asyncio

            async def _preload() -> None:
                from app.llm import warm_up_configured

                try:
                    await warm_up_configured()
                except Exception as e:
                    logger.debug("model preload skipped: {}", e)

            asyncio.create_task(_preload())
        yield
        # Shutdown: free the models we (or LM Studio's JIT) loaded so they don't
        # stay pinned in VRAM after the app closes. Bounded so a slow/missing
        # LM Studio can't stall exit (the desktop window only waits ~6s).
        if getattr(s, "unload_on_exit", True):
            import asyncio

            try:
                from app.llm import unload_all_models

                ok, msg = await asyncio.wait_for(unload_all_models(), timeout=8.0)
                logger.info("unload on exit: {}", msg)
            except Exception as e:
                logger.debug("unload on exit skipped: {}", e)

    fastapi_app = FastAPI(
        title=__app_name__,
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    fastapi_app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        same_site="lax",
        https_only=False,
        # 90 days — this is a local desktop app, the cookie lives on the same
        # machine. Long expiry means the user only sees the login screen once.
        max_age=90 * 24 * 3600,
    )

    cors_origins = ["http://localhost", "http://127.0.0.1"]
    if s.allow_lan:
        cors_origins.append("*")
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastapi_app.include_router(api_router)
    register_ui(fastapi_app)

    logger.info(
        "{} v{} ready — listening on http://{}:{}",
        __app_name__,
        __version__,
        s.host,
        s.port,
    )
    return fastapi_app


# ---------------------------------------------------------------------------
# Stage 3 — uvicorn launch
# ---------------------------------------------------------------------------


def _launch_headless() -> None:
    """Server-only launch — no window. Used in tests and via LDI_NO_WINDOW=1."""
    import uvicorn

    from app.config import get_settings

    s = get_settings()
    host = "0.0.0.0" if s.allow_lan else s.host
    fastapi_app = create_app()

    uvicorn.run(
        fastapi_app,
        host=host,
        port=s.port,
        reload=False,
        log_level=s.log_level.lower(),
        log_config=None,
        access_log=False,
    )


def _launch() -> None:
    """Pick the right launch mode (native window vs. headless server)."""
    from app.desktop import can_open_window, run_with_window

    if can_open_window():
        run_with_window()
    else:
        _launch_headless()


def run() -> None:
    """Public entry point.

    Wraps :func:`_launch` so any failure produces a crash dump on disk.
    """
    _ensure_console_streams()
    stage = "startup"
    try:
        stage = "build/serve"
        _launch()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return
    except BaseException as e:
        path = _write_crash_dump(e, stage=stage)
        # Re-raise so dev terminals still see the error.
        if not _is_frozen() and sys.stderr is not None:
            try:
                traceback.print_exc()
                if path:
                    print(f"\nCrash dump: {path}", file=sys.stderr)
            except Exception:
                pass
        # In frozen mode we exit with a distinct code so launcher / tests
        # can detect "we crashed, look in the dump file".
        sys.exit(70)


# Allow `python -m app.main` and PyInstaller's `<script>.exe`
if __name__ == "__main__":
    # Required for PyInstaller-frozen apps that may spawn subprocesses
    # (NiceGUI's background tasks, chromadb's workers).
    multiprocessing.freeze_support()
    run()
