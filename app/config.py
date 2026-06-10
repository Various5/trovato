"""Application configuration.

Loads settings from environment variables (.env), with sensible Windows-first
defaults. A user-editable ``settings.json`` lives next to the database in the
app data directory and overrides selected values at runtime.
"""

from __future__ import annotations

import json
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "LocalDocIntelligence"
APP_DISPLAY_NAME = "LocalDoc Intelligence"


def default_data_dir() -> Path:
    """Resolve the default per-user data directory."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / APP_NAME


class Settings(BaseSettings):
    """Environment-derived configuration. All fields use the ``LDI_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="LDI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Server ----
    host: str = "127.0.0.1"
    port: int = 8765
    allow_lan: bool = False
    # Mark the session cookie ``Secure`` so the browser only sends it over HTTPS.
    # OFF by default: enabling it while serving plain HTTP makes the browser drop
    # the cookie entirely (you can't stay logged in). Only turn it on when the
    # app sits behind HTTPS (a reverse proxy / tunnel).
    secure_cookies: bool = False
    secret_key: str = ""
    debug: bool = False
    log_level: str = "INFO"

    # ---- Paths ----
    data_dir: str = ""
    db_url: str = ""

    # ---- LM Studio ----
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_api_key: str = "lm-studio"
    chat_model: str = ""
    vision_model: str = ""
    embedding_model: str = ""
    # Language for vision image descriptions: "auto" follows each document's own
    # detected language (so German PDFs get German descriptions that match German
    # queries + highlight), or force a code like "en"/"de".
    vision_language: str = "auto"
    # Preference for the hardware-aware model auto-picker (Settings → Auto-pick):
    # "fastest" | "balanced" | "max" (quality). See app/services/model_advisor.py.
    model_quality: str = "balanced"
    # Use the chat model to generate real SUBJECT topic tags during indexing
    # (in addition to the heuristic system tags). Off by default: it adds an LLM
    # call per document, so it's opt-in. The Tags page also has a manual
    # "generate topics" backfill that works regardless of this flag.
    llm_topics_enabled: bool = False
    # Preload the configured models into LM Studio at startup so they're hot
    # before the first scan/chat (warm_up_configured). Set false to keep boot
    # light and rely on just-in-time loading instead.
    preload_models: bool = True
    # Unload all LM Studio models when the app shuts down (`lms unload --all`),
    # so we don't leave the user's models pinned in VRAM after closing.
    unload_on_exit: bool = True

    # ---- OCR ----
    ocr_backend: str = "tesseract"  # "tesseract" | "paddle"
    tesseract_cmd: str = ""
    ocr_lang: str = "eng+deu"

    # ---- Indexing ----
    chunk_size: int = 1100
    chunk_overlap: int = 150
    ocr_min_text_chars: int = 60  # below this we trigger OCR for a page
    max_file_size_mb: int = 512
    # Performance auto-tuning. ``performance_profile`` is auto|low|balanced|high;
    # ``auto`` sizes the pipeline to the detected CPU/RAM/GPU (see
    # app/services/hardware.py). ``parallel_workers`` is a manual override:
    # 0 (default) defers to the profile, any positive value pins the heavy
    # worker count.
    performance_profile: str = "auto"
    parallel_workers: int = 0
    # SQLite busy-timeout (ms): how long a connection waits for a contended
    # write lock before giving up. The indexer serializes its own writers with
    # a process-global lock; this is the cross-process / checkpoint backstop.
    sqlite_busy_timeout_ms: int = 10_000

    # ---- Resolved at runtime (not env-bound) ----
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_path / "localdoc.db"

    @property
    def chroma_path(self) -> Path:
        return self.data_path / "chroma"

    @property
    def cache_path(self) -> Path:
        return self.data_path / "cache"

    @property
    def backups_path(self) -> Path:
        return self.data_path / "backups"

    @property
    def logs_path(self) -> Path:
        return self.data_path / "logs"

    @property
    def settings_json_path(self) -> Path:
        return self.data_path / "settings.json"

    @property
    def effective_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite:///{self.db_path.as_posix()}"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_path,
            self.chroma_path,
            self.cache_path,
            self.cache_path / "images",
            self.cache_path / "pages",
            self.backups_path,
            self.logs_path,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def ensure_secret_key(self) -> None:
        """Load the master secret key from a dedicated keystore file (never
        settings.json, never a backup). Same value + derivations as before, so
        existing encrypted secrets/sessions keep working. Migrates a legacy
        plaintext key out of settings.json on first run after the change."""
        if self.secret_key:
            return
        from app.utils.keystore import read_secret_key, write_secret_key

        keyfile = self.data_path / "secret.key"
        key = read_secret_key(keyfile)
        if not key:
            legacy = load_user_settings().get("secret_key")
            if legacy:
                key = legacy
                # Stop exporting it — drop it from settings.json (which is backed up).
                current = load_user_settings()
                current.pop("secret_key", None)
                _settings_json_path().write_text(
                    json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            else:
                key = secrets.token_urlsafe(48)
            write_secret_key(keyfile, key)
        self.secret_key = key


def _settings_json_path() -> Path:
    """Resolve the settings.json path without triggering cached get_settings().

    Called from load_user_settings during the *first* get_settings() call —
    so it must not loop back through get_settings().
    """
    s = Settings()
    return s.settings_json_path


def load_user_settings() -> dict[str, Any]:
    """Read the user-editable settings.json (created lazily).

    Must not call get_settings() — it is itself invoked from get_settings()'s
    first run, which would recurse forever.
    """
    p = _settings_json_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_settings(updates: dict[str, Any]) -> dict[str, Any]:
    p = _settings_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_user_settings()
    current.update(updates)
    p.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance (call ``get_settings.cache_clear()`` to refresh)."""
    s = Settings()
    s.ensure_dirs()
    # overlay persisted user settings for selected fields
    user = load_user_settings()
    for key in (
        "lmstudio_base_url",
        "chat_model",
        "vision_model",
        "embedding_model",
        "vision_language",
        "model_quality",
        "llm_topics_enabled",
        "preload_models",
        "unload_on_exit",
        "ocr_backend",
        "tesseract_cmd",
        "ocr_lang",
        "chunk_size",
        "chunk_overlap",
        "ocr_min_text_chars",
        "performance_profile",
        "parallel_workers",
        "host",
        "port",
        "allow_lan",
        "secure_cookies",
        "log_level",
    ):
        if key in user and user[key] not in (None, ""):
            setattr(s, key, user[key])
    s.ensure_secret_key()
    return s
