"""Mint a LocalDoc Intelligence license key (vendor-side).

Signs a license payload with your PRIVATE key (created by scripts/gen_keypair.py)
and prints the key to hand to a customer. The shipped app holds only the public
key and cannot mint keys.

    python -m scripts.gen_license --licensee "Acme Corp <ops@acme.com>"
    python -m scripts.gen_license --licensee "Jane" --expires 2027-01-31
    python -m scripts.gen_license --licensee "Jane" --plan pro --key PATH

The license token is printed to stdout (the only stdout line, so it is easy to
pipe/copy); a human-readable summary goes to stderr.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from app.services.licensing import PAYLOAD_VERSION, make_token, verify_token


def default_key_path() -> Path:
    return Path.home() / ".localdoc-license" / "signing_key.pem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint a license key.")
    ap.add_argument("--licensee", required=True, help="Who the key is for (name and/or email).")
    ap.add_argument("--expires", default=None, help="Expiry date YYYY-MM-DD (omit = perpetual).")
    ap.add_argument("--plan", default="standard", help="Plan/tier label embedded in the key.")
    ap.add_argument("--id", default=None, help="License id (default: random uuid4).")
    ap.add_argument("--key", type=Path, default=default_key_path(), help="Private key PEM path.")
    args = ap.parse_args(argv)

    if not args.key.exists():
        print(
            f"Private key not found at {args.key}. Run `python -m scripts.gen_keypair` first.",
            file=sys.stderr,
        )
        return 2

    if args.expires:
        try:
            date.fromisoformat(args.expires)
        except ValueError:
            print(f"--expires must be YYYY-MM-DD, got {args.expires!r}", file=sys.stderr)
            return 2

    priv = serialization.load_pem_private_key(args.key.read_bytes(), password=None)

    payload: dict = {
        "v": PAYLOAD_VERSION,
        "id": args.id or str(uuid.uuid4()),
        "licensee": args.licensee,
        "plan": args.plan,
        "issued": date.today().isoformat(),
    }
    if args.expires:
        payload["expires"] = args.expires

    token = make_token(payload, priv)

    # Sanity check that the app will accept what we just minted.
    status = verify_token(token)
    if not status.active:
        print(
            f"INTERNAL ERROR: freshly minted key did not verify ({status.reason}). "
            "Is PUBLIC_KEY_HEX in app/services/licensing.py in sync with this private key?",
            file=sys.stderr,
        )
        return 1

    print(token)
    exp = payload.get("expires", "perpetual")
    print(
        f"\n# licensee={args.licensee!r} plan={args.plan} expires={exp} id={payload['id']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
