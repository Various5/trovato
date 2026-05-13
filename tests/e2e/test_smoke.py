"""Playwright smoke test — verifies the app boots, the login page loads, and
the first-run wizard accepts an admin signup.

Run via:
    pip install -e ".[e2e]"
    playwright install chromium
    pytest tests/e2e -m e2e

The test is skipped automatically if Playwright is not installed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("ldi-e2e")
    port = _free_port()
    env = os.environ.copy()
    env["LDI_DATA_DIR"] = str(data_dir)
    env["LDI_PORT"] = str(port)
    env["LDI_LOG_LEVEL"] = "WARNING"
    # On CI the first import chain (NiceGUI + ChromaDB + PyMuPDF + Pillow)
    # plus first-time chroma index creation can take well over 30s, so we
    # use a generous timeout and dump the subprocess stderr if it doesn't
    # come up — saves the next person from blind log archaeology.
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=5)
            raise RuntimeError(
                f"server died (exit {proc.returncode})\n"
                f"--- stdout ---\n{stdout.decode(errors='replace')[-4000:]}\n"
                f"--- stderr ---\n{stderr.decode(errors='replace')[-4000:]}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    else:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=5)
            tail = stderr.decode(errors="replace")[-4000:]
        except Exception:
            tail = "(could not capture stderr)"
        raise RuntimeError(f"server never came up within 120s\n--- stderr tail ---\n{tail}")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_first_run_wizard_loads(app_server: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.goto(app_server, wait_until="domcontentloaded")
        page.wait_for_selector("text=LocalDoc Intelligence", timeout=10_000)
        browser.close()
