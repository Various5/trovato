from pathlib import Path

from app.backup import create_backup, restore_backup
from app.database import init_db


def test_create_and_restore_settings_backup(tmp_path: Path) -> None:
    init_db()
    out = tmp_path / "b.zip"
    res = create_backup(["settings", "chats", "memory"], output_path=out)
    assert out.exists()
    assert res.size_bytes > 0
    info = restore_backup(out, components=["settings"], make_safety_copy=False)
    assert "errors" in info
