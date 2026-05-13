# PyInstaller spec for LocalDoc Intelligence.
# Run via `python installer/build.py`.

# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


# ---------------------------------------------------------------------------
# Project root: the spec file lives in installer/, so root is its parent.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH injected by PyInstaller)
ENTRY = str(PROJECT_ROOT / "app" / "main.py")


hidden: list[str] = []
datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []


# Third-party libraries with dynamic imports / data files. ``collect_all`` is
# the catch-all hammer; if it can't find the package it returns empty tuples.
for pkg in (
    "nicegui",
    "chromadb",
    "fitz",          # PyMuPDF
    "pdfplumber",
    "tiktoken",      # encoders shipped as binary blobs
    "tiktoken_ext",  # ext registry
    "argon2",
    "loguru",
    "httpx",
    "sqlalchemy",
    "sqlmodel",
    "pydantic",
    "pydantic_settings",
    "starlette",
    "fastapi",
    "watchfiles",
    "cryptography",
    "zstandard",
    "humanize",
    "PIL",
    "cv2",
    "numpy",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception:
        pass

# Our own package: every submodule pre-discovered so dynamic imports
# (``from app.services.X import Y`` inside route handlers, etc.) always
# resolve in the frozen bundle.
hidden += collect_submodules("app")

# SQLAlchemy registers dialects via entry points — bundle them explicitly.
hidden += collect_submodules("sqlalchemy.dialects")
hidden += collect_submodules("sqlalchemy.connectors")

# uvicorn loops + httptools / h11 protocols are picked up via entry points.
hidden += collect_submodules("uvicorn")
hidden += [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

# Metadata files some libraries read at runtime (importlib.metadata).
for pkg in (
    "nicegui",
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlmodel",
    "sqlalchemy",
    "pydantic",
    "chromadb",
    "tiktoken",
    "loguru",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# tiktoken ships its tokenizer data outside the importable tree.
try:
    datas += collect_data_files("tiktoken_ext", include_py_files=True)
except Exception:
    pass


a = Analysis(
    [ENTRY],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hidden)),
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Cut size — these would only be pulled in by transitive specs.
        "matplotlib",
        "tkinter",
        "PySide6",
        "PyQt5",
        "PyQt6",
        "IPython",
        "jupyter",
        "notebook",
        "pandas.tests",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalDocIntelligence",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LocalDocIntelligence",
)
