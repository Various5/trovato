"""Pytest configuration — isolate data dir per test session."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir() -> str:
    tmp = tempfile.mkdtemp(prefix="ldi-test-")
    os.environ["LDI_DATA_DIR"] = tmp
    os.environ["LDI_SECRET_KEY"] = "test-secret-key"
    # Clear cached settings
    from app.config import get_settings

    get_settings.cache_clear()
    return tmp
