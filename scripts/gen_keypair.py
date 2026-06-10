"""Generate the Ed25519 keypair used to sign LocalDoc Intelligence license keys.

Run this ONCE. It writes the PRIVATE key to a file OUTSIDE the repo (keep it
safe and backed up — anyone with it can mint valid license keys) and prints the
PUBLIC key, which you embed in ``app/services/licensing.py`` (``PUBLIC_KEY_HEX``).

    python -m scripts.gen_keypair                 # -> ~/.localdoc-license/signing_key.pem
    python -m scripts.gen_keypair --out PATH
    python -m scripts.gen_keypair --force         # overwrite an existing key (invalidates old licenses!)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def default_key_path() -> Path:
    return Path.home() / ".localdoc-license" / "signing_key.pem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the license signing keypair.")
    ap.add_argument("--out", type=Path, default=default_key_path(), help="Private key output path (PEM).")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing private key.")
    args = ap.parse_args(argv)

    out: Path = args.out
    if out.exists() and not args.force:
        print(
            f"Refusing to overwrite existing private key at {out}\n"
            "Use --force only if you really mean to — it INVALIDATES every key "
            "signed with the old private key.",
            file=sys.stderr,
        )
        return 2

    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pem)
    try:
        out.chmod(0o600)
    except OSError:
        pass

    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    print("Keypair generated.")
    print(f"  Private key (KEEP SECRET, back it up): {out}")
    print("  Public key — embed this in app/services/licensing.py:\n")
    print(f'    PUBLIC_KEY_HEX = "{pub_hex}"\n')
    print("Next: paste PUBLIC_KEY_HEX into app/services/licensing.py, then mint keys")
    print("with `python -m scripts.gen_license --licensee ...`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
