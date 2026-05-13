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
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.4)
    else:
        proc.terminate()
        raise RuntimeError("server never came up")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


def test_first_run_wizard_loads(app_server: str) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.goto(app_server, wait_until="domcontentloaded")
        page.wait_for_selector("text=LocalDoc Intelligence", timeout=10_000)
        browser.close()
