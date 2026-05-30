"""Ad-hoc page-render smoke test: boot headless, first-run, GET every page.

Catches request-time errors in NiceGUI page bodies (i18n keys, closure scope,
the new tag-browse + perf cards) that import-time checks miss. Not a unit test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx

PORT = "18799"
BASE = f"http://127.0.0.1:{PORT}"
PAGES = [
    "/",
    "/search",
    "/search?tag=has:images",
    "/chat",
    "/documents",
    "/sources",
    "/tags",
    "/diagnostics",
    "/settings",
    "/compare",
    "/about",
]


def main() -> int:
    env = dict(os.environ)
    env["LDI_NO_WINDOW"] = "1"
    env["LDI_PORT"] = PORT
    env["LDI_DATA_DIR"] = tempfile.mkdtemp(prefix="ldi-pagesmoke-")
    env["LDI_SECRET_KEY"] = "pagesmoke"
    env["LDI_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen([sys.executable, "-m", "app.main"], env=env)
    try:
        deadline = time.time() + 60
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"server exited early code={proc.returncode}")
                return 1
            try:
                if httpx.get(f"{BASE}/api/health/ping", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not up:
            print("server never answered ping")
            return 1

        s = httpx.Client(base_url=BASE, timeout=15, follow_redirects=True)
        creds = {"username": "smoke", "password": "smoketest1234"}
        s.post("/api/auth/first-run", json=creds)
        s.post("/api/auth/login", json=creds)

        failed = []
        for p in PAGES:
            try:
                r = s.get(p)
                tag = "OK " if r.status_code == 200 else f"!! {r.status_code}"
                print(f"  {tag} {p}")
                if r.status_code != 200:
                    failed.append(f"{p} ({r.status_code})")
            except Exception as e:
                print(f"  !! ERR {p}: {e}")
                failed.append(f"{p} ({e})")
        if failed:
            print("FAILED:", ", ".join(failed))
            return 1
        print("ALL PAGES 200")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
