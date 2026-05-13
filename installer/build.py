"""Build script — produces a frozen Windows binary using PyInstaller.

Usage:
    python installer/build.py

Output:
    dist/LocalDocIntelligence/LocalDocIntelligence.exe (+ side-files)

Afterwards run Inno Setup on installer/localdoc.iss to produce the .exe
installer in installer/Output/.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "installer" / "localdoc.spec"


def _have_pyinstaller() -> bool:
    return importlib.util.find_spec("PyInstaller") is not None


def main() -> int:
    if not _have_pyinstaller():
        print("PyInstaller not installed — run: pip install -e .[build]", file=sys.stderr)
        return 2

    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    build = ROOT / "build"
    if build.exists():
        shutil.rmtree(build)

    # Use the current interpreter's `python -m PyInstaller` rather than relying
    # on a `pyinstaller` binary being on PATH. The latter often isn't present
    # in fresh venvs / CI runners.
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)]
    print(">>", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
