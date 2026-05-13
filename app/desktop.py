"""Native desktop window mode.

Strategy: run uvicorn on a background thread and open a pywebview window that
loads ``http://127.0.0.1:<port>``. On Windows the embedded engine is WebView2
(Edge Chromium), which is pre-installed on Windows 10+ — no Chromium ships
inside the bundle.

When the window is closed we signal uvicorn to exit cleanly so the process
goes away (no orphan server). Falling back to the headless server is just one
env var away (``LDI_NO_WINDOW=1``).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from app.utils.logging import logger


def can_open_window() -> bool:
    """Detect whether a native window is *possible* in this environment.

    Returns False if:
      - the user explicitly opted out with LDI_NO_WINDOW=1
      - pywebview isn't installed
      - we're on a headless host (no DISPLAY / WSL without WSLg)
    """
    if os.environ.get("LDI_NO_WINDOW") == "1":
        return False
    try:
        import webview  # noqa: F401
    except ImportError:
        logger.warning("pywebview not installed; falling back to browser mode")
        return False
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    return True


def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    """Block until ``host:port`` accepts TCP connections (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                # Server bound; now verify the app is actually serving.
                try:
                    url = f"http://{host}:{port}/api/health/ping"
                    urllib.request.urlopen(url, timeout=2.0).read()
                    return True
                except urllib.error.URLError:
                    pass
        except OSError:
            pass
        time.sleep(0.25)
    return False


def run_with_window() -> None:
    """Boot the server in a background thread, open a native window."""
    import uvicorn
    import webview

    from app import __app_name__
    from app.config import get_settings
    from app.main import create_app

    s = get_settings()
    host = "0.0.0.0" if s.allow_lan else s.host
    public_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    fastapi_app = create_app()

    config = uvicorn.Config(
        fastapi_app,
        host=host,
        port=s.port,
        log_level=s.log_level.lower(),
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, name="ldi-server", daemon=True)
    server_thread.start()

    if not _wait_for_port(public_host, s.port, timeout=90.0):
        server.should_exit = True
        raise RuntimeError(f"server failed to come up on {public_host}:{s.port}")

    logger.info("opening native window at http://{}:{}", public_host, s.port)

    # Pick a sensible startup geometry that fits on a 1366x768 laptop too.
    window = webview.create_window(
        title=__app_name__,
        url=f"http://{public_host}:{s.port}",
        width=1280,
        height=820,
        min_size=(960, 640),
        confirm_close=False,
        text_select=True,
    )

    def _on_closing() -> None:
        logger.info("window closing — shutting down server")
        server.should_exit = True

    window.events.closing += _on_closing  # type: ignore[attr-defined]

    # ``http_server=False`` because we have our own.
    webview.start()

    # If we get here, the window was closed by the user. Give uvicorn a moment
    # to shut down cleanly, then bail out.
    server.should_exit = True
    server_thread.join(timeout=6.0)
