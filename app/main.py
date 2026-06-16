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
        candidates.append(Path(appdata) / "Trovato")
    candidates.append(Path.home() / ".trovato")
    candidates.append(Path(os.environ.get("TEMP", "")) / "Trovato")
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
            f.write("Trovato — crash dump\n")
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

            from app.services.indexer import (
                backfill_image_chunk_sources,
                backfill_tag_quality,
            )

            _aio.create_task(_aio.to_thread(backfill_image_chunk_sources))
            # One-time: clean noisy pre-existing auto-tags (drop bare doc-type +
            # has:dates/has:amounts, namespace sensitivity). Idempotent.
            _aio.create_task(_aio.to_thread(backfill_tag_quality))
        except Exception as e:
            logger.debug("backfills not scheduled: {}", e)
        # Preload the configured models into LM Studio so they're hot before the
        # first scan/chat, then heal the vector store if the embedding model
        # changed under it. Detached + best-effort: never blocks boot, and if LM
        # Studio is down it just logs and moves on (the heal retries next boot).
        import asyncio

        async def _preload_and_heal() -> None:
            if getattr(s, "preload_models", True):
                try:
                    from app.llm import warm_up_configured

                    await warm_up_configured()
                except Exception as e:
                    logger.debug("model preload skipped: {}", e)
            try:
                from app.services.indexer import heal_vector_store_if_model_changed

                n = await heal_vector_store_if_model_changed()
                if n:
                    logger.info("vector heal: re-embedded {} document(s)", n)
            except Exception as e:
                logger.debug("vector heal skipped: {}", e)

        asyncio.create_task(_preload_and_heal())
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

    # Derive a DISTINCT cookie-signing key from the master secret so a leak of
    # the session-cookie key doesn't reveal the at-rest encryption key
    # (sha256(secret_key)) or the media-token key (its own salt). One-time effect:
    # cookies signed by an older build are invalidated, so users sign in once.
    import hashlib as _hashlib

    _cookie_key = _hashlib.sha256((s.secret_key + ":session-cookie-v1").encode("utf-8")).hexdigest()
    fastapi_app.add_middleware(
        SessionMiddleware,
        secret_key=_cookie_key,
        same_site="lax",
        # Secure flag is opt-in (Settings → Network) for HTTPS deployments; the
        # cookie is always HttpOnly (Starlette default), so client JS can't read
        # it. Setting Secure on plain HTTP would drop the cookie, so it defaults
        # off for the localhost desktop case.
        https_only=s.secure_cookies,
        # Stateless signed cookie → no server-side revocation beyond a password
        # change, so keep the window short (default 7 days, configurable) to
        # bound replay of a captured cookie on an exposed deployment.
        max_age=max(1, s.session_max_age_days) * 24 * 3600,
    )

    # CORS: the app is single-origin (the UI is served from the same host as the
    # API), so same-origin requests never need CORS approval. We therefore only
    # allow the localhost dev origins and NEVER a credentialed wildcard — the old
    # `["*"]` + allow_credentials reflected any attacker Origin back with
    # Access-Control-Allow-Credentials, enabling credentialed cross-site reads on
    # sibling-domain/tunnel deployments.
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://127.0.0.1"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from starlette.responses import JSONResponse

    # Request-body size cap. The app takes no file uploads (sources are
    # filesystem paths), so every API body is small JSON. Without a cap an
    # UNAUTHENTICATED client could POST a multi-GB body to a license-open
    # endpoint (/api/auth/login, /recover) — FastAPI buffers it into RAM before
    # any auth/rate-limit runs — and OOM the single-process server. Reject early
    # on Content-Length; the few large bodies (chat) stay well under the cap.
    _MAX_BODY_BYTES = 4 * 1024 * 1024  # 4 MiB

    @fastapi_app.middleware("http")
    async def _body_size_limit(request, call_next):  # type: ignore[no-untyped-def]
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            cl = request.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > _MAX_BODY_BYTES:
                        return JSONResponse({"detail": "request body too large"}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
        return await call_next(request)

    # Server-side license gate. The UI redirect alone is not enough — every
    # /api data route must refuse to serve content until the app is activated,
    # else a logged-in-but-unlicensed user could pull the whole library straight
    # over HTTP. Auth/health/about/docs stay open so login, liveness and the
    # version check keep working while the app is locked.
    _license_open = ("/api/auth", "/api/health", "/api/about", "/api/docs", "/api/openapi.json")

    @fastapi_app.middleware("http")
    async def _license_gate(request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(_license_open):
            from app.services import licensing

            if not licensing.is_activated():
                return JSONResponse({"detail": "license required"}, status_code=403)
        return await call_next(request)

    # Security middleware: CSRF protection + hardening headers. Browsers attach
    # an Origin header on cross-site requests; a mismatch on a state-changing
    # /api method is a CSRF attempt (the victim's cookie would otherwise be
    # replayed by an attacker page), so we reject it. Non-browser API clients
    # omit Origin and carry no ambient cookie, so they're unaffected.
    import urllib.parse as _urlparse

    _unsafe_methods = {"POST", "PUT", "PATCH", "DELETE"}

    # Content-Security-Policy, shipped in REPORT-ONLY mode first: it reports
    # would-be violations to the browser console WITHOUT enforcing, so it can't
    # break the NiceGUI live-reload WebSocket, the PDF/media viewer, or the
    # inline Vue/Quasar bootstrap while we confirm the policy is clean. Once
    # verified in the running app, flip the header name below to the enforcing
    # "Content-Security-Policy". NiceGUI requires 'unsafe-inline'/'unsafe-eval'
    # (Vue/Quasar) and a ws/wss connect-src; all app assets, media and fonts are
    # same-origin ('self'), so no CDN/host allowances are needed.
    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'self'"
    )

    @fastapi_app.middleware("http")
    async def _security(request, call_next):  # type: ignore[no-untyped-def]
        if request.method in _unsafe_methods and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and _urlparse.urlsplit(origin).netloc != request.headers.get("host", ""):
                return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy-Report-Only", _CSP)
        return resp

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
