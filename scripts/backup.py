"""CLI: create a backup.

Usage:
    python -m scripts.backup --output backup.zip --include db,vector,chats,memory,settings
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.backup import BACKUP_COMPONENTS, create_backup


def main() -> int:
    p = argparse.ArgumentParser(description="Create a LocalDoc Intelligence backup")
    p.add_argument("--output", "-o", default=None, help="Output ZIP path")
    p.add_argument(
        "--include",
        default="db,vector,chats,memory,settings",
        help=f"Comma-separated components: {','.join(BACKUP_COMPONENTS)}",
    )
    p.add_argument("--password", default=None, help="Encrypt with password")
    p.add_argument("--originals", action="store_true", help="Include original PDFs")
    args = p.parse_args()

    comps = [c.strip() for c in args.include.split(",") if c.strip()]
    res = create_backup(
        comps,
        output_path=Path(args.output) if args.output else None,
        encrypt_password=args.password,
        include_originals=args.originals,
    )
    print(f"Backup created: {res.path} ({res.size_bytes:,} bytes)")
    print(f"Components: {res.components}")
    print(f"Encrypted: {res.encrypted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
