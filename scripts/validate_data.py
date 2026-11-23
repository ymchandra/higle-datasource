#!/usr/bin/env python3
"""Validate data/*.json against schema/higle/higle-day.schema.json (plus a few extra integrity checks).

Usage: python scripts/validate_data.py [--data-dir data] [--require-all]
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "higle" / "higle-day.schema.json"
LEAP_YEAR = 2024


def expected_keys() -> list[str]:
    return [
        f"{m:02d}-{d:02d}"
        for m in range(1, 13)
        for d in range(1, calendar.monthrange(LEAP_YEAR, m)[1] + 1)
    ]


def validate_file(path: Path, validator: Draft202012Validator) -> list[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"not valid JSON: {exc}"]
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    ]
    if errors:
        return errors
    if doc["date"] != path.stem:
        errors.append(f"date {doc['date']!r} does not match file name {path.name}")
    ids = [item["id"] for item in doc["items"]]
    if len(ids) != len(set(ids)):
        errors.append("duplicate item ids")
    if not doc["items"]:
        errors.append("items is empty")
    elif len(doc["items"]) >= 3 and sum(i["is_featured"] for i in doc["items"]) < 3:
        errors.append("fewer than 3 featured items")
    return errors


def validate_manifest(data_dir: Path, day_files: list[Path]) -> list[str]:
    path = data_dir / "manifest.json"
    if not path.exists():
        return ["manifest.json is missing"] if day_files else []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"manifest.json: not valid JSON: {exc}"]
    errors = []
    for key in ("generated_at", "total_days", "schema_version", "days"):
        if key not in manifest:
            errors.append(f"manifest.json: missing {key!r}")
    if errors:
        return errors
    if manifest["total_days"] != len(day_files) or len(manifest["days"]) != len(day_files):
        errors.append(f"manifest.json: total_days={manifest['total_days']} but {len(day_files)} day files exist")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--require-all", action="store_true", help="fail unless all 366 day files exist")
    args = ap.parse_args(argv)

    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    files = sorted(args.data_dir.glob("[0-1][0-9]-[0-3][0-9].json"))
    failures = 0
    for path in files:
        for error in validate_file(path, validator):
            failures += 1
            print(f"FAIL {path.name}: {error}")
    for error in validate_manifest(args.data_dir, files):
        failures += 1
        print(f"FAIL {error}")
    if args.require_all:
        missing = sorted(set(expected_keys()) - {p.stem for p in files})
        if missing:
            failures += len(missing)
            print(f"FAIL missing {len(missing)} day files: {', '.join(missing)}")

    print(f"validated {len(files)} day files: {'OK' if not failures else f'{failures} problem(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
