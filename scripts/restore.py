"""CLI: restore a backup.

Usage:
    python -m scripts.restore --input backup.zip
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.backup import restore_backup


def main() -> int:
    p = argparse.ArgumentParser(description="Restore a LocalDoc Intelligence backup")
    p.add_argument("--input", "-i", required=True, help="Backup archive path")
    p.add_argument("--password", default=None)
    p.add_argument("--components", default=None, help="Comma-separated components to restore")
    args = p.parse_args()

    components = [c.strip() for c in args.components.split(",") if c.strip()] if args.components else None
    res = restore_backup(Path(args.input), components=components, password=args.password)
    print(f"Restored: {res['restored']}")
    if res["errors"]:
        print(f"Errors: {res['errors']}")
    print(f"Manifest: {res['manifest']}")
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
