"""Tests for the offline license verification module.

A throwaway Ed25519 keypair is generated per test and the embedded
``PUBLIC_KEY_HEX`` is monkeypatched to its public half, so these never depend on
the real (vendor-held) private key.
"""

from __future__ import annotations

from datetime import date

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import licensing


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    monkeypatch.setattr(licensing, "PUBLIC_KEY_HEX", pub_hex)
    return priv


def _mk(priv, **extra):
    payload = {
        "v": licensing.PAYLOAD_VERSION,
        "id": "test-id",
        "licensee": "Jane Doe <jane@example.com>",
        "plan": "standard",
        "issued": "2026-01-01",
    }
    payload.update(extra)
    return licensing.make_token(payload, priv)


def test_perpetual_key_is_valid(signer):
    st = licensing.verify_token(_mk(signer))
    assert st.active and st.reason == "ok"
    assert st.info is not None
    assert st.info.licensee == "Jane Doe <jane@example.com>"
    assert st.info.is_perpetual


def test_future_expiry_valid_and_boundary(signer):
    tok = _mk(signer, expires="2027-01-31")
    assert licensing.verify_token(tok, today=date(2027, 1, 31)).active  # expiry day inclusive
    assert not licensing.verify_token(tok, today=date(2027, 2, 1)).active


def test_expired_key(signer):
    st = licensing.verify_token(_mk(signer, expires="2020-01-01"))
    assert not st.active and st.reason == "expired"
    assert st.info is not None  # payload still parsed for display


def test_tampered_signature_is_invalid(signer):
    tok = _mk(signer)
    st = licensing.verify_token(tok[:-6] + "AAAAAA")
    assert not st.active and st.reason == "invalid"


def test_wrong_key_is_invalid(signer):
    other = Ed25519PrivateKey.generate()
    tok = licensing.make_token(
        {"v": 1, "id": "x", "licensee": "X", "plan": "p", "issued": "2026-01-01"}, other
    )
    assert licensing.verify_token(tok).reason == "invalid"


@pytest.mark.parametrize(
    "bad,reason",
    [("", "none"), ("   ", "none"), ("nope", "malformed"), ("LDI1.a.b", "malformed"), ("X.y.z", "malformed")],
)
def test_bad_tokens(signer, bad, reason):
    assert licensing.verify_token(bad).reason == reason


def test_days_remaining(signer):
    tok = _mk(signer, expires="2027-01-10")
    st = licensing.verify_token(tok, today=date(2027, 1, 1))
    assert licensing.days_remaining(st.info, today=date(2027, 1, 1)) == 9
    perp = licensing.verify_token(_mk(signer))
    assert licensing.days_remaining(perp.info) is None


def test_activate_persists_and_deactivate(signer, tmp_path, monkeypatch):
    lic = tmp_path / "license.lic"
    monkeypatch.setattr(licensing, "license_file_path", lambda: lic)

    # An invalid key must not be persisted and must not activate.
    assert not licensing.activate("garbage").active
    assert not lic.exists()
    assert not licensing.is_activated()

    # A valid key activates and is written verbatim.
    tok = _mk(signer)
    assert licensing.activate(tok).active
    assert lic.read_text(encoding="utf-8").strip() == tok
    assert licensing.is_activated()
    assert licensing.current_status().info.licensee.startswith("Jane")

    # Deactivation removes the file and re-locks.
    licensing.deactivate()
    assert not lic.exists()
    assert not licensing.is_activated()
