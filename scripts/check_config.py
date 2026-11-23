#!/usr/bin/env python3
"""Syntax/consistency checks for sources/sources.json and every JSON Schema under schema/."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_SOURCE_KEYS = {"id", "name", "enabled", "priority", "adapter", "base_url", "schema_path"}


def main() -> int:
    errors: list[str] = []

    for path in sorted((ROOT / "schema").rglob("*.json")):
        try:
            Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, SchemaError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: {str(exc).splitlines()[0]}")

    try:
        config = json.loads((ROOT / "sources" / "sources.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        errors.append(f"sources/sources.json: invalid JSON: {exc}")
        config = {"sources": []}
    seen_ids, seen_priorities = set(), set()
    for source in config.get("sources", []):
        missing = REQUIRED_SOURCE_KEYS - source.keys()
        if missing:
            errors.append(f"source {source.get('id')!r}: missing keys {sorted(missing)}")
            continue
        if source["id"] in seen_ids or source["priority"] in seen_priorities:
            errors.append(f"source {source['id']!r}: duplicate id or priority")
        seen_ids.add(source["id"])
        seen_priorities.add(source["priority"])
        if not (ROOT / source["schema_path"]).is_file():
            errors.append(f"source {source['id']!r}: schema_path {source['schema_path']} not found")
        if not (ROOT / "scripts" / "adapters" / f"{source['adapter']}.py").is_file():
            errors.append(f"source {source['id']!r}: adapter {source['adapter']} not found")

    for error in errors:
        print(f"FAIL {error}")
    print("config OK" if not errors else f"{len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
