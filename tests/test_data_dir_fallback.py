"""Non-destructive legacy data-dir fallback after the v0.7.0 Trovato rename.

The rename shipped no data migration, so an existing install's library still
lives under the old ``LocalDocIntelligence`` dir / ``localdoc.db`` filename. The
renamed build must keep finding it instead of opening an empty library.
"""

from __future__ import annotations

import app.config as cfg
from app.config import Settings


def test_db_path_prefers_trovato_then_localdoc(tmp_path) -> None:
    d = tmp_path / "lib"
    d.mkdir()
    # Fresh dir (no db): a new install creates trovato.db.
    assert Settings(data_dir=str(d)).db_path.name == "trovato.db"
    # Only the legacy db present: use it (don't ignore the real library).
    (d / "localdoc.db").write_text("x")
    assert Settings(data_dir=str(d)).db_path.name == "localdoc.db"
    # Both present (already migrated): the current name wins.
    (d / "trovato.db").write_text("x")
    assert Settings(data_dir=str(d)).db_path.name == "trovato.db"


def test_explicit_data_dir_is_respected(tmp_path) -> None:
    d = tmp_path / "explicit"
    d.mkdir()
    assert Settings(data_dir=str(d)).data_path == d  # no fallback when set


def test_resolved_default_falls_back_to_legacy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cfg, "_appdata_root", lambda: tmp_path)
    try:
        # Neither dir has a db -> current (Trovato), i.e. a true fresh install.
        cfg.resolved_default_data_dir.cache_clear()
        assert cfg.resolved_default_data_dir() == tmp_path / "Trovato"
        # Only the legacy dir has a db -> fall back to it (read in place).
        cfg.resolved_default_data_dir.cache_clear()
        legacy = tmp_path / "LocalDocIntelligence"
        legacy.mkdir()
        (legacy / "localdoc.db").write_text("x")
        assert cfg.resolved_default_data_dir() == legacy
        # The current dir later gains a db -> current wins, never hidden.
        cfg.resolved_default_data_dir.cache_clear()
        cur = tmp_path / "Trovato"
        cur.mkdir()
        (cur / "trovato.db").write_text("x")
        assert cfg.resolved_default_data_dir() == cur
    finally:
        cfg.resolved_default_data_dir.cache_clear()  # don't leak the patched dir
