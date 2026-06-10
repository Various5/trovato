"""secret_key keystore: round-trip, migration out of settings.json (value
preserved), credential-store continuity, and backup never exporting the key."""

from __future__ import annotations

import json
import zipfile

import pytest


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LDI_DATA_DIR", str(tmp_path))
    # The conftest pins LDI_SECRET_KEY, which would short-circuit ensure_secret_key;
    # drop it here so the keystore/migration path actually runs.
    monkeypatch.delenv("LDI_SECRET_KEY", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()  # don't leak the tmp data dir into other tests


def test_keystore_roundtrip(tmp_path):
    from app.utils.keystore import read_secret_key, write_secret_key

    kf = tmp_path / "secret.key"
    write_secret_key(kf, "abc-123-XYZ")
    assert read_secret_key(kf) == "abc-123-XYZ"
    assert read_secret_key(tmp_path / "missing.key") is None


def test_secret_key_migrates_out_of_settings_json(isolated):
    sj = isolated / "settings.json"
    sj.write_text(json.dumps({"secret_key": "legacy-xyz", "chat_model": "m"}))
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.secret_key == "legacy-xyz"  # value preserved → old ciphertext still decrypts
    on_disk = json.loads(sj.read_text())
    assert "secret_key" not in on_disk  # stripped from the (backed-up) settings.json
    assert on_disk["chat_model"] == "m"  # unrelated keys untouched
    assert (isolated / "secret.key").exists()
    get_settings.cache_clear()
    assert get_settings().secret_key == "legacy-xyz"  # now loaded from the keystore


def test_credential_store_survives_migration(isolated):
    (isolated / "settings.json").write_text(json.dumps({"secret_key": "stable-key-1"}))
    from app.config import get_settings
    from app.database import init_db

    get_settings.cache_clear()
    get_settings()  # triggers migration
    init_db()
    from app.utils.secret_store import get_secret, put_secret

    put_secret("ks-src-test", {"password": "p@ss"})
    get_settings.cache_clear()  # force a fresh key load from the keystore
    assert get_secret("ks-src-test") == {"password": "p@ss"}


def test_backup_never_exports_secret_key(isolated):
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    # Even if a legacy key somehow lingers in settings.json, the backup strips it.
    s.settings_json_path.write_text(json.dumps({"secret_key": "should-not-export", "chat_model": "m"}))
    from app.backup.service import create_backup

    out = isolated / "b.zip"
    create_backup(["settings"], output_path=out)
    with zipfile.ZipFile(out) as zf:
        data = json.loads(zf.read("settings/settings.json"))
    assert "secret_key" not in data
    assert data["chat_model"] == "m"
