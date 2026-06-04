"""Tests for signed media tokens authorizing PDF / page-image URLs.

These tokens let browser-issued requests (a PDF opened in a new tab, an <img>
src) authenticate without the NiceGUI session cookie — the fix for the repeated
"open in browser → login required" report.
"""

from __future__ import annotations

from app.auth.security import make_media_token, verify_media_token


def test_round_trip_returns_uid() -> None:
    token = make_media_token(42)
    assert verify_media_token(token) == 42


def test_tampered_token_is_rejected() -> None:
    token = make_media_token(7)
    assert verify_media_token(token + "x") is None
    assert verify_media_token("garbage") is None
    assert verify_media_token("") is None


def test_expired_token_is_rejected() -> None:
    token = make_media_token(7)
    # negative max_age → any token (age >= 0) is treated as expired
    assert verify_media_token(token, max_age=-1) is None
