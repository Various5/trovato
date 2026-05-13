# PyInstaller spec for LocalDoc Intelligence.
# Run via `python installer/build.py`.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules
import sys


hidden = []
datas = []
binaries = []

for pkg in ("nicegui", "chromadb", "fitz", "pdfplumber"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception:
        pass

hidden += collect_submodules("uvicorn")
hidden += collect_submodules("sqlmodel")


a = Analysis(
    ["..\\app\\main.py"] if sys.platform == "win32" else ["../app/main.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LocalDocIntelligence",
)
