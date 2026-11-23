#!/usr/bin/env python3
"""Higle data pipeline: fetch -> validate -> transform -> merge/deduplicate -> validate -> write.

Usage:
    python scripts/pipeline.py --mode rolling [--date MM-DD]
    python scripts/pipeline.py --mode full [--start-month 1] [--end-month 12] [--force]
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser
from jsonschema import Draft202012Validator, ValidationError

import deduplicator
from adapters import BaseAdapter, load_adapter
from http_client import FetchError, RateLimitedError, get_json

log = logging.getLogger("pipeline")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0.0"
MAX_CONSECUTIVE_FAILURES = 5
LEAP_YEAR = 2024


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class SourceRuntime:
    config: dict[str, Any]
    adapter: BaseAdapter
    validator: Draft202012Validator
    consecutive_failures: int = 0
    disabled_reason: str | None = None

    @property
    def id(self) -> str:
        return self.config["id"]

    def record_failure(self, reason: str, *, fatal: bool = False) -> None:
        self.consecutive_failures += 1
        if fatal or self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.disabled_reason = reason
            log.warning("source %s disabled for the rest of this run: %s", self.id, reason)

    def record_success(self) -> None:
        self.consecutive_failures = 0


@dataclass
class Pipeline:
    data_dir: Path
    sources: list[SourceRuntime]
    user_agent: str
    output: dict[str, Any]
    schema_validator: Draft202012Validator
    sleep: Any = time.sleep
    results: dict[str, str] = field(default_factory=dict)
    wrote_any: bool = False
    skip_existing: bool = False

    # ---- fetching -------------------------------------------------------
    def _fetch_source(self, source: SourceRuntime, month: int, day: int) -> list[dict[str, Any]] | None:
        """Fetch + schema-validate + transform one source for one day. None on any failure."""
        if source.disabled_reason:
            return None
        try:
            payload = source.adapter.fetch(month, day, user_agent=self.user_agent, get_json=get_json)
            source.validator.validate(payload)
            items = source.adapter.transform(payload, month, day)
        except RateLimitedError as exc:
            log.warning("%s %02d-%02d: %s", source.id, month, day, exc)
            source.record_failure("rate limited", fatal=True)
            return None
        except ValidationError as exc:
            log.warning("%s %02d-%02d: schema validation failed: %s", source.id, month, day, exc.message[:200])
            source.record_failure("schema validation failures")
            return None
        except (FetchError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s %02d-%02d: %s: %s", source.id, month, day, type(exc).__name__, exc)
            source.record_failure(f"repeated errors ({exc})")
            return None
        finally:
            self.sleep(source.config.get("rate_limit_delay_sec", 0))
        source.record_success()
        return items

    def fetch_all(self, month: int, day: int) -> dict[str, list[dict[str, Any]]]:
        """Fetch every active source in parallel; returns {source_id: items} for healthy ones."""
        active = [s for s in self.sources if not s.disabled_reason]
        if not active:
            return {}
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            fetched = list(pool.map(lambda s: self._fetch_source(s, month, day), active))
        return {s.id: items for s, items in zip(active, fetched) if items}

    # ---- building / writing --------------------------------------------
    def build_day(
        self, month: int, day: int, by_source: dict[str, list[dict[str, Any]]], previous: dict | None
    ) -> dict[str, Any]:
        ordered = [item for s in self.sources for item in by_source.get(s.id, [])]
        out = self.output
        items = deduplicator.process(
            ordered,
            month,
            day,
            threshold=out.get("similarity_threshold", deduplicator.DEFAULT_THRESHOLD),
            max_items=out.get("max_items_per_day", 100),
            quotas=out.get("category_quotas"),
            sort_order=out.get("sort_order", "desc"),
            min_featured=out.get("min_featured", 3),
            max_featured=out.get("max_featured", 5),
        )
        if previous:
            deduplicator.stabilize_ids(items, previous.get("items", []), out.get("similarity_threshold", 75))
        return {
            "date": f"{month:02d}-{day:02d}",
            "display_date": f"{calendar.month_name[month]} {day}",
            "last_updated": utc_now(),
            "sources_used": [s.id for s in self.sources if s.id in by_source],
            "items": [{k: item[k] for k in EVENT_KEYS} for item in items],
        }

    def process_day(self, month: int, day: int, *, force: bool = False) -> str:
        key = f"{month:02d}-{day:02d}"
        path = self.data_dir / f"{key}.json"
        previous = self._read_existing(path)

        if previous and previous.get("items") and not force and self.skip_existing:
            return self._done(key, "skipped-existing")

        by_source = self.fetch_all(month, day)
        if not by_source:
            log.warning("%s: no source returned data; keeping %s", key, "existing file" if previous else "no file")
            return self._done(key, "failed-kept-existing" if previous else "failed-no-data")

        document = self.build_day(month, day, by_source, previous)
        try:
            self.schema_validator.validate(document)
        except ValidationError as exc:
            log.error("%s: generated document is invalid, not writing: %s", key, exc.message[:200])
            return self._done(key, "invalid-not-written")
        if not document["items"]:
            return self._done(key, "failed-kept-existing" if previous else "failed-no-data")

        if previous and previous.get("items"):
            old_sources, new_sources = set(previous.get("sources_used", [])), set(document["sources_used"])
            if new_sources < old_sources and not force:
                log.warning("%s: only %s answered (previously %s); keeping existing file", key,
                            sorted(new_sources), sorted(old_sources))
                return self._done(key, "degraded-kept-existing")
            if _comparable(previous) == _comparable(document):
                return self._done(key, "unchanged")

        write_json(self.data_dir / f"{key}.json", document)
        self.wrote_any = True
        return self._done(key, "written")

    def _done(self, key: str, status: str) -> str:
        self.results[key] = status
        log.info("%s: %s", key, status)
        return status

    @staticmethod
    def _read_existing(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            data = load_json(path)
        except (OSError, ValueError):
            log.warning("%s is unreadable; treating as missing", path)
            return None
        return data if isinstance(data, dict) else None


EVENT_KEYS = (
    "id", "year", "year_display", "category", "title", "summary",
    "image_url", "source_url", "is_featured", "providers",
)


def _comparable(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "last_updated"}


# ---- manifest -----------------------------------------------------------
def update_manifest(data_dir: Path, *, force_touch: bool = False) -> dict[str, Any]:
    """Rebuild data/manifest.json from the day files. generated_at only moves when content changes."""
    days: dict[str, dict[str, Any]] = {}
    for path in sorted(data_dir.glob("[0-1][0-9]-[0-3][0-9].json")):
        try:
            doc = load_json(path)
            days[path.stem] = {"last_updated": doc["last_updated"], "event_count": len(doc["items"])}
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("manifest: skipping unreadable %s", path.name)
    manifest_path = data_dir / "manifest.json"
    old = None
    if manifest_path.exists():
        try:
            old = load_json(manifest_path)
        except (OSError, ValueError):
            old = None
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "version": SCHEMA_VERSION,
        "generated_at": (old or {}).get("generated_at", utc_now()),
        "total_days": len(days),
        "total_events": sum(d["event_count"] for d in days.values()),
        "days": days,
    }
    if old is None or force_touch or _comparable_manifest(old) != _comparable_manifest(manifest):
        manifest["generated_at"] = utc_now()
        write_json(manifest_path, manifest)
    return manifest


def _comparable_manifest(m: dict) -> dict:
    return {k: v for k, v in m.items() if k != "generated_at"}


# ---- setup & CLI --------------------------------------------------------
def build_pipeline(
    root: Path = ROOT, data_dir: Path | None = None, only_sources: list[str] | None = None
) -> Pipeline:
    config = load_json(root / "sources" / "sources.json")
    runtimes = []
    for source in sorted(config["sources"], key=lambda s: s["priority"]):
        if not source.get("enabled", True) or (only_sources and source["id"] not in only_sources):
            continue
        schema = load_json(root / source["schema_path"])
        runtimes.append(SourceRuntime(source, load_adapter(source), Draft202012Validator(schema)))
    higle_schema = load_json(root / "schema" / "higle" / "higle-day.schema.json")
    return Pipeline(
        data_dir=data_dir or root / "data",
        sources=runtimes,
        user_agent=config["user_agent"],
        output=config.get("output", {}),
        schema_validator=Draft202012Validator(higle_schema),
    )


def parse_month_day(value: str) -> tuple[int, int]:
    """Parse 'MM-DD' (also accepts anything dateutil understands, e.g. 'Nov 23')."""
    parsed = date_parser.parse(value, default=datetime(LEAP_YEAR, 1, 1))
    return parsed.month, parsed.day


def rolling_days(center: tuple[int, int] | None = None, today: date | None = None) -> list[tuple[int, int]]:
    """Yesterday, today and tomorrow (UTC) around ``center`` (default: today UTC)."""
    today = today or datetime.now(timezone.utc).date()
    if center:
        year = today.year if calendar.isleap(today.year) or center != (2, 29) else LEAP_YEAR
        today = date(year, *center)
    return [((today + timedelta(days=d)).month, (today + timedelta(days=d)).day) for d in (-1, 0, 1)]


def full_days(start_month: int = 1, end_month: int = 12) -> list[tuple[int, int]]:
    return [
        (m, d)
        for m in range(start_month, end_month + 1)
        for d in range(1, calendar.monthrange(LEAP_YEAR, m)[1] + 1)
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["rolling", "full"], default="rolling")
    ap.add_argument("--date", help="rolling mode: centre date MM-DD (default: today UTC)")
    ap.add_argument("--start-month", type=int, default=1)
    ap.add_argument("--end-month", type=int, default=12)
    ap.add_argument("--force", action="store_true", help="overwrite existing files (full mode skips populated days otherwise)")
    ap.add_argument("--sources", help="comma separated source ids to use (default: all enabled)")
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not (1 <= args.start_month <= args.end_month <= 12):
        ap.error("need 1 <= start-month <= end-month <= 12")

    pipeline = build_pipeline(data_dir=args.data_dir, only_sources=args.sources.split(",") if args.sources else None)
    pipeline.data_dir.mkdir(parents=True, exist_ok=True)
    if not pipeline.sources:
        log.error("no enabled sources")
        return 1

    if args.mode == "rolling":
        days = rolling_days(parse_month_day(args.date) if args.date else None)
        log.info("rolling update for %s", ", ".join(f"{m:02d}-{d:02d}" for m, d in days))
    else:
        days = full_days(args.start_month, args.end_month)
        pipeline.skip_existing = not args.force
        log.info("full backfill of %d days (force=%s)", len(days), args.force)

    for month, day in days:
        pipeline.process_day(month, day, force=args.force)
    update_manifest(pipeline.data_dir)

    summary: dict[str, list[str]] = {}
    for key, status in pipeline.results.items():
        summary.setdefault(status, []).append(key)
    for status, keys in sorted(summary.items()):
        log.info("summary %-22s %3d %s", status, len(keys), ", ".join(keys) if len(keys) <= 20 else "")
    for source in pipeline.sources:
        if source.disabled_reason:
            log.warning("source %s was disabled during the run: %s", source.id, source.disabled_reason)

    healthy = {"written", "unchanged", "skipped-existing", "degraded-kept-existing"}
    return 0 if any(s in healthy for s in pipeline.results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
