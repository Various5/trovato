"""Build script — produces a frozen Windows binary using PyInstaller.

Usage:
    python installer/build.py

Output:
    dist/LocalDocIntelligence/LocalDocIntelligence.exe (+ side-files)

Afterwards run Inno Setup on installer/localdoc.iss to produce the .exe
installer in installer/Output/.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "installer" / "localdoc.spec"


def main() -> int:
    if shutil.which("pyinstaller") is None:
        print("pyinstaller not found — run: pip install -e .[build]", file=sys.stderr)
        return 2
    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    build = ROOT / "build"
    if build.exists():
        shutil.rmtree(build)
    cmd = ["pyinstaller", "--clean", "--noconfirm", str(SPEC)]
    print(">>", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
