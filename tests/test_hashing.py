from pathlib import Path

from app.utils.hashing import sha256_bytes, sha256_file, sha256_text


def test_sha256_bytes_known() -> None:
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sha256_text_matches_bytes() -> None:
    assert sha256_text("hello") == sha256_bytes(b"hello")


def test_sha256_file(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    assert sha256_file(p) == sha256_bytes(b"hello")
