#!/usr/bin/env python3
"""Higle data pipeline: fetch -> validate -> transform -> merge/deduplicate -> validate -> write.

Usage:
    python scripts/pipeline.py --mode rolling [--date MM-DD]
    python scripts/pipeline.py --mode full [--start-month 1] [--end-month 12] [--force]
    python scripts/pipeline.py --mode annotate   # re-apply licence blocks + image policy to existing files
    python scripts/pipeline.py --mode dyk        # add/refresh did_you_know in existing files (no event refetch)
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dateutil import parser as date_parser
from jsonschema import Draft202012Validator, ValidationError

import deduplicator
import legal
from adapters import BaseAdapter, load_adapter
from http_client import FetchError, RateLimitedError, get_json
from image_licenses import ImageLicenseResolver, apply_image_policy

log = logging.getLogger("pipeline")

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "1.0.0"
MAX_CONSECUTIVE_FAILURES = 5
LEAP_YEAR = 2024
DYK_SOURCE_ID = "wikimedia_dyk"
DYK_DEFAULTS = {"min_items": 3, "max_items": 5, "fetch_window_days": 2}


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
    config: dict[str, Any] = field(default_factory=dict)
    image_resolver: ImageLicenseResolver | None = None
    dyk: SourceRuntime | None = None
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    sleep: Any = time.sleep
    results: dict[str, str] = field(default_factory=dict)
    image_stats: Counter = field(default_factory=Counter)
    dyk_image_stats: Counter = field(default_factory=Counter)
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

    # ---- did you know -----------------------------------------------------
    @property
    def dyk_settings(self) -> dict[str, Any]:
        return {**DYK_DEFAULTS, **self.output.get("did_you_know", {})}

    def dyk_feed_date(self, month: int, day: int) -> date | None:
        """The calendar date to ask the featured feed for, or None when it is too far from today to have a dyk array."""
        window = timedelta(days=self.dyk_settings["fetch_window_days"])
        for year in (self.today.year - 1, self.today.year, self.today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if abs(candidate - self.today) <= window:
                return candidate
        return None

    def fetch_dyk(self, month: int, day: int) -> list[dict[str, Any]]:
        """Wikipedia 'Did you know' hooks for a day; [] when unavailable (never raises)."""
        source = self.dyk
        feed_date = self.dyk_feed_date(month, day)
        if source is None or source.disabled_reason or feed_date is None:
            return []
        try:
            payload = source.adapter.fetch(month, day, user_agent=self.user_agent, get_json=get_json, year=feed_date.year)
            source.validator.validate(payload)
            facts = source.adapter.transform(payload, month, day)
        except RateLimitedError as exc:
            log.warning("%s %02d-%02d: %s", source.id, month, day, exc)
            source.record_failure("rate limited", fatal=True)
            return []
        except ValidationError as exc:
            log.warning("%s %02d-%02d: schema validation failed: %s", source.id, month, day, exc.message[:200])
            source.record_failure("schema validation failures")
            return []
        except (FetchError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("%s %02d-%02d: %s: %s", source.id, month, day, type(exc).__name__, exc)
            source.record_failure(f"repeated errors ({exc})")
            return []
        finally:
            self.sleep(source.config.get("rate_limit_delay_sec", 0))
        source.record_success()
        return facts

    def _attach_page_images(self, facts: list[dict[str, Any]]) -> None:
        """Thumbnail of the hook's article via the REST summary API (the feed itself has no images)."""
        template = (self.dyk.config if self.dyk else {}).get("page_summary_url")
        if not template or not self.image_resolver or self.image_resolver.disabled:
            return
        for fact in facts:
            title = fact.get("page_title")
            if fact.get("image_url") or not title:
                continue
            url = template.replace("{title}", quote(title.replace(" ", "_"), safe=""))
            try:
                summary = get_json(url, user_agent=self.user_agent)
                fact["image_url"] = ((summary.get("thumbnail") or {}).get("source")) if isinstance(summary, dict) else None
            except FetchError as exc:
                log.debug("no page image for %s: %s", title, exc)
            finally:
                self.sleep(self.dyk.config.get("rate_limit_delay_sec", 0))

    def compute_did_you_know(
        self, month: int, day: int, items: list[dict[str, Any]], previous: dict | None
    ) -> list[dict[str, Any]]:
        """Feed hooks (or those kept from the previous file) topped up by the heuristic extractor, images licence-checked."""
        settings = self.dyk_settings
        previous_facts = (previous or {}).get("did_you_know") or []
        retained = [f for f in previous_facts if DYK_SOURCE_ID in f.get("providers", [])]
        feed = self.fetch_dyk(month, day)
        known = {f["id"]: f for f in previous_facts}
        for fact in feed:
            fact.setdefault("id", deduplicator.fun_fact_extractor.fact_id(month, day, fact["fact"]))
            if fact["id"] in known and known[fact["id"]].get("image_url"):
                fact["image_url"], fact["image_credit"] = known[fact["id"]]["image_url"], known[fact["id"]].get("image_credit")
        facts = deduplicator.build_did_you_know(
            items,
            month,
            day,
            feed_facts=feed,
            retained_facts=retained,
            min_items=settings["min_items"],
            max_items=settings["max_items"],
            threshold=self.output.get("similarity_threshold", deduplicator.DEFAULT_THRESHOLD),
        )
        self._attach_page_images([f for f in facts if DYK_SOURCE_ID in f.get("providers", [])])
        stats = apply_image_policy(facts, self.image_resolver, list(items) + list(previous_facts))
        self.dyk_image_stats.update(stats)
        return facts

    def dyk_day(self, month: int, day: int, *, force: bool = False) -> str:
        """Add/refresh did_you_know in an existing file without refetching the event sources."""
        key = f"{month:02d}-{day:02d}"
        previous = self._read_existing(self.data_dir / f"{key}.json")
        if not previous or not previous.get("items"):
            return self._done(key, "dyk-skipped-no-file")
        if previous.get("did_you_know") is not None and not force and self.dyk_feed_date(month, day) is None:
            return self._done(key, "dyk-present")
        facts = self.compute_did_you_know(month, day, previous["items"], previous)
        document = self.assemble(month, day, previous.get("sources_used", []), previous["items"], facts)
        try:
            self.schema_validator.validate(document)
        except ValidationError as exc:
            log.error("%s: document with did_you_know is invalid, not writing: %s", key, exc.message[:200])
            return self._done(key, "invalid-not-written")
        if _comparable(previous) == _comparable(document):
            return self._done(key, "unchanged")
        status = "dyk-updated" if previous.get("did_you_know") is not None else "dyk-added"
        write_json(self.data_dir / f"{key}.json", document)
        self.wrote_any = True
        return self._done(key, status)

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
        self.finalize_items(month, day, items, (previous or {}).get("items", []))
        facts = self.compute_did_you_know(month, day, items, previous)
        return self.assemble(month, day, [s.id for s in self.sources if s.id in by_source], items, facts)

    def finalize_items(
        self, month: int, day: int, items: list[dict[str, Any]], previous_items: list[dict[str, Any]]
    ) -> Counter:
        """Licence-check images and make sure every item links to an originating article."""
        fallback = legal.day_article_url(month, day)
        for item in items:
            item["source_url"] = item.get("source_url") or fallback
        stats = apply_image_policy(items, self.image_resolver, previous_items)
        self.image_stats.update(stats)
        return stats

    def assemble(
        self,
        month: int,
        day: int,
        sources_used: list[str],
        items: list[dict[str, Any]],
        did_you_know: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sources_used = [s for s in sources_used if s != DYK_SOURCE_ID]
        if any(DYK_SOURCE_ID in f.get("providers", []) for f in did_you_know or []):
            sources_used.append(DYK_SOURCE_ID)
        document: dict[str, Any] = {
            "date": f"{month:02d}-{day:02d}",
            "display_date": f"{calendar.month_name[month]} {day}",
            "last_updated": utc_now(),
            "sources_used": sources_used,
        }
        if self.config.get("legal"):
            document.update(legal.day_legal_block(self.config, sources_used))
        if did_you_know is not None:
            document["did_you_know"] = [
                {k: fact.get(k) for k in FACT_KEYS if k in FACT_REQUIRED or fact.get(k) is not None}
                for fact in did_you_know
            ]
        document["items"] = [{k: item.get(k) for k in EVENT_KEYS} for item in items]
        return document

    def annotate_day(self, month: int, day: int, *, force: bool = False) -> str:
        """Re-apply licence/attribution blocks and the image policy to an existing file without refetching."""
        key = f"{month:02d}-{day:02d}"
        previous = self._read_existing(self.data_dir / f"{key}.json")
        if not previous or not previous.get("items"):
            return self._done(key, "annotate-skipped-no-file")
        if previous.get("license") and not force:
            return self._done(key, "already-annotated")
        items = [dict(item) for item in previous["items"]]
        stats = self.finalize_items(month, day, items, previous["items"])
        if stats["unresolved"]:
            log.warning("%s: %d image licences could not be looked up; leaving file untouched", key, stats["unresolved"])
            return self._done(key, "annotate-unresolved-kept-existing")
        document = self.assemble(month, day, previous.get("sources_used", []), items, previous.get("did_you_know"))
        try:
            self.schema_validator.validate(document)
        except ValidationError as exc:
            log.error("%s: annotated document is invalid, not writing: %s", key, exc.message[:200])
            return self._done(key, "invalid-not-written")
        write_json(self.data_dir / f"{key}.json", document)
        self.wrote_any = True
        return self._done(key, "annotated")

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
            old_sources = set(previous.get("sources_used", [])) - {DYK_SOURCE_ID}
            new_sources = set(document["sources_used"]) - {DYK_SOURCE_ID}
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
    "image_url", "image_credit", "source_url", "is_featured", "providers",
)


FACT_KEYS = ("id", "fact", "tag", "image_url", "image_credit", "source_url", "providers")
FACT_REQUIRED = ("id", "fact", "tag", "image_url", "source_url")


def _comparable(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "last_updated"}


# ---- manifest -----------------------------------------------------------
def update_manifest(
    data_dir: Path, *, force_touch: bool = False, legal_block: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Rebuild data/manifest.json from the day files. generated_at only moves when content changes."""
    days: dict[str, dict[str, Any]] = {}
    for path in sorted(data_dir.glob("[0-1][0-9]-[0-3][0-9].json")):
        try:
            doc = load_json(path)
            days[path.stem] = {
                "last_updated": doc["last_updated"],
                "event_count": len(doc["items"]),
                "did_you_know_count": len(doc.get("did_you_know") or []),
            }
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
        **(legal_block or {}),
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
    root: Path = ROOT,
    data_dir: Path | None = None,
    only_sources: list[str] | None = None,
    resolve_images: bool = True,
) -> Pipeline:
    config = load_json(root / "sources" / "sources.json")
    runtimes = []
    dyk = None
    for source in sorted(config["sources"], key=lambda s: s["priority"]):
        if source.get("kind") == "did_you_know":
            # Not an events source: feeds did_you_know only, so --sources does not apply to it.
            if source.get("enabled", True):
                schema = load_json(root / source["schema_path"])
                dyk = SourceRuntime(source, load_adapter(source), Draft202012Validator(schema))
            continue
        # An explicit --sources list may opt in to a source that is disabled in the registry.
        wanted = source["id"] in only_sources if only_sources else source.get("enabled", True)
        if not wanted:
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
        config=config,
        dyk=dyk,
        image_resolver=ImageLicenseResolver(user_agent=config["user_agent"]) if resolve_images else None,
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
    ap.add_argument("--mode", choices=["rolling", "full", "annotate", "dyk"], default="rolling")
    ap.add_argument("--date", help="rolling mode (or dyk mode, to limit it to those 3 days): centre date MM-DD (default: today UTC)")
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

    if args.mode == "rolling" or (args.mode == "dyk" and args.date):
        days = rolling_days(parse_month_day(args.date) if args.date else None)
        log.info("rolling update for %s", ", ".join(f"{m:02d}-{d:02d}" for m, d in days))
    else:
        days = full_days(args.start_month, args.end_month)
        pipeline.skip_existing = not args.force
        log.info("%s of %d days (force=%s)", {"full": "full backfill", "annotate": "annotation", "dyk": "did_you_know update"}[args.mode], len(days), args.force)

    for month, day in days:
        if args.mode == "annotate":
            pipeline.annotate_day(month, day, force=args.force)
        elif args.mode == "dyk":
            pipeline.dyk_day(month, day, force=args.force)
        else:
            pipeline.process_day(month, day, force=args.force)
    update_manifest(
        pipeline.data_dir,
        legal_block=legal.manifest_legal_block(pipeline.config) if pipeline.config.get("legal") else None,
    )

    summary: dict[str, list[str]] = {}
    for key, status in pipeline.results.items():
        summary.setdefault(status, []).append(key)
    for status, keys in sorted(summary.items()):
        log.info("summary %-22s %3d %s", status, len(keys), ", ".join(keys) if len(keys) <= 20 else "")
    if pipeline.image_stats:
        log.info("images: %s", ", ".join(f"{k}={v}" for k, v in sorted(pipeline.image_stats.items())))
    if pipeline.dyk_image_stats:
        log.info("did_you_know images: %s", ", ".join(f"{k}={v}" for k, v in sorted(pipeline.dyk_image_stats.items())))
    for source in pipeline.sources + ([pipeline.dyk] if pipeline.dyk else []):
        if source.disabled_reason:
            log.warning("source %s was disabled during the run: %s", source.id, source.disabled_reason)

    healthy = {"written", "unchanged", "skipped-existing", "degraded-kept-existing", "annotated", "already-annotated",
               "dyk-added", "dyk-updated", "dyk-present"}
    return 0 if any(s in healthy for s in pipeline.results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
