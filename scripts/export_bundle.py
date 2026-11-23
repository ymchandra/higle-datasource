#!/usr/bin/env python3
"""Zip data/*.json (day files + manifest) into higle-offline-bundle.zip for the app's assets/data/ folder.

Usage: python scripts/export_bundle.py [--data-dir data] [--output higle-offline-bundle.zip]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--output", type=Path, default=ROOT / "higle-offline-bundle.zip")
    args = ap.parse_args(argv)

    files = sorted(args.data_dir.glob("*.json"))
    if not files:
        print(f"no JSON files in {args.data_dir}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, arcname=path.name)
    print(f"wrote {args.output} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
