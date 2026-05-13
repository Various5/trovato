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

    # ---- OCR ----
    ocr_backend: str = "tesseract"  # "tesseract" | "paddle"
    tesseract_cmd: str = ""
    ocr_lang: str = "eng+deu"

    # ---- Indexing ----
    chunk_size: int = 1100
    chunk_overlap: int = 150
    ocr_min_text_chars: int = 60  # below this we trigger OCR for a page
    max_file_size_mb: int = 512
    parallel_workers: int = 2

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
        """Persist a generated secret key inside settings.json if one isn't set."""
        if self.secret_key:
            return
        key = load_user_settings().get("secret_key")
        if not key:
            key = secrets.token_urlsafe(48)
            save_user_settings({"secret_key": key})
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
        "ocr_backend",
        "tesseract_cmd",
        "ocr_lang",
        "chunk_size",
        "chunk_overlap",
        "ocr_min_text_chars",
        "parallel_workers",
        "allow_lan",
        "log_level",
    ):
        if key in user and user[key] not in (None, ""):
            setattr(s, key, user[key])
    s.ensure_secret_key()
    return s
