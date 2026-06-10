"""Offline license verification.

License keys are short signed tokens::

    TRV1.<base64url(payload)>.<base64url(signature)>

The payload is canonical JSON (``{v,id,licensee,plan,issued,expires?}``) and the
signature is Ed25519 over the *exact* payload bytes the token carries — so there
is no re-canonicalisation ambiguity between the signer and the verifier.

Only the PUBLIC key ships in the app, so the app can VERIFY keys but never mint
them. Keys are unforgeable without the private key, which lives solely on the
vendor's machine (see ``scripts/gen_keypair.py`` + ``scripts/gen_license.py``).

Verification is a local signature + expiry check — no network call, so the
product's "100% offline" promise is preserved. Note: offline expiry can be
sidestepped by setting the system clock back; that's an accepted trade-off for
a local-first, honesty-based product (and the repo is open source anyway).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# Embedded signing public key (hex of 32 raw bytes). Generated once by
# scripts/gen_keypair.py; the matching private key never ships with the app.
PUBLIC_KEY_HEX = "8322487ca522bd3d5f534cc49fe992e87edd9309af974a774348bca6c32b2874"

TOKEN_PREFIX = "TRV1"
PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class LicenseInfo:
    licensee: str
    plan: str
    issued: str | None
    expires: str | None  # ISO date 'YYYY-MM-DD', or None for a perpetual key
    id: str
    raw: dict[str, Any]

    @property
    def is_perpetual(self) -> bool:
        return not self.expires


@dataclass(frozen=True)
class LicenseStatus:
    active: bool
    reason: str  # "ok" | "none" | "expired" | "invalid" | "malformed"
    info: LicenseInfo | None = None


# ---- low-level encode/decode ------------------------------------------------
def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes — used both to sign and to verify."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_token(payload: dict[str, Any], private_key: Ed25519PrivateKey) -> str:
    """Sign ``payload`` and assemble a license token (used by scripts/gen_license.py)."""
    body = canonical_bytes(payload)
    sig = private_key.sign(body)
    return f"{TOKEN_PREFIX}.{_b64u_encode(body)}.{_b64u_encode(sig)}"


def _public_key(public_key_hex: str | None = None) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex or PUBLIC_KEY_HEX))


# ---- verification -----------------------------------------------------------
def verify_token(
    token: str,
    *,
    public_key_hex: str | None = None,
    today: date | None = None,
) -> LicenseStatus:
    """Verify a token's signature and expiry. Never raises — returns a status."""
    token = (token or "").strip()
    if not token:
        return LicenseStatus(False, "none")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return LicenseStatus(False, "malformed")
    try:
        body = _b64u_decode(parts[1])
        sig = _b64u_decode(parts[2])
    except Exception:
        return LicenseStatus(False, "malformed")
    try:
        _public_key(public_key_hex).verify(sig, body)
    except InvalidSignature:
        return LicenseStatus(False, "invalid")
    except Exception:
        return LicenseStatus(False, "invalid")
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return LicenseStatus(False, "malformed")

    info = LicenseInfo(
        licensee=str(payload.get("licensee", "")),
        plan=str(payload.get("plan", "standard")),
        issued=payload.get("issued"),
        expires=payload.get("expires"),
        id=str(payload.get("id", "")),
        raw=payload,
    )
    # Validate the (signed) payload version. Defence-in-depth: the version lives
    # inside the signed bytes, but the TRV1 prefix is not signed, so refuse to
    # honour a payload whose version we don't understand.
    if payload.get("v") != PAYLOAD_VERSION:
        return LicenseStatus(False, "invalid", info)
    if info.expires:
        try:
            exp = date.fromisoformat(str(info.expires))
        except ValueError:
            return LicenseStatus(False, "malformed", info)
        if (today or date.today()) > exp:
            return LicenseStatus(False, "expired", info)
    return LicenseStatus(True, "ok", info)


def days_remaining(info: LicenseInfo, *, today: date | None = None) -> int | None:
    """Whole days until expiry (negative if past); ``None`` for a perpetual key."""
    if not info.expires:
        return None
    try:
        exp = date.fromisoformat(str(info.expires))
    except ValueError:
        return None
    return (exp - (today or date.today())).days


# ---- storage / high-level API -----------------------------------------------
def license_file_path() -> Path:
    # Imported lazily so this module has no import-time dependency on config.
    from app.config import get_settings

    return get_settings().data_path / "license.lic"


def read_stored_token() -> str:
    try:
        p = license_file_path()
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def current_status() -> LicenseStatus:
    """Status of the currently-stored license (re-read + re-verified each call)."""
    return verify_token(read_stored_token())


def is_activated() -> bool:
    return current_status().active


def activate(token: str) -> LicenseStatus:
    """Verify a pasted token; persist it only if it is valid. Returns the status."""
    status = verify_token(token)
    if status.active:
        p = license_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token.strip(), encoding="utf-8")
    return status


def deactivate() -> None:
    """Remove the stored license (re-locks the app)."""
    try:
        license_file_path().unlink(missing_ok=True)
    except OSError:
        pass
