"""Licence/attribution blocks written into every day file and into data/manifest.json."""

from __future__ import annotations

import calendar
from typing import Any


def day_article_url(month: int, day: int) -> str:
    """The English Wikipedia date article (e.g. .../November_23) that "On this day" entries originate from."""
    return f"https://en.wikipedia.org/wiki/{calendar.month_name[month]}_{day}"


def _source_credit(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": source["id"],
        "name": source["name"],
        "url": source.get("homepage_url"),
        "license": source.get("license"),
        "license_url": source.get("license_url"),
        "attribution": source.get("attribution", ""),
    }


def _attribution(legal: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    parts = [s["attribution"] for s in sources if s.get("attribution")]
    parts.append(legal["attribution_suffix"])
    return " ".join(parts)


def day_legal_block(config: dict[str, Any], sources_used: list[str]) -> dict[str, Any]:
    """license / attribution / sources / modifications for a day built from ``sources_used``."""
    legal = config["legal"]
    by_id = {s["id"]: s for s in config["sources"]}
    used = [by_id[i] for i in sources_used if i in by_id]
    return {
        "license": dict(legal["dataset_license"]),
        "attribution": _attribution(legal, used),
        "sources": [_source_credit(s) for s in used],
        "modifications": legal["modifications"],
        "notice_url": legal["notice_url"],
    }


def manifest_legal_block(config: dict[str, Any]) -> dict[str, Any]:
    """Same blocks for the manifest, covering every enabled source plus the image policy."""
    legal = config["legal"]
    enabled = [s for s in sorted(config["sources"], key=lambda s: s["priority"]) if s.get("enabled", True)]
    block = day_legal_block(config, [s["id"] for s in enabled])
    block["image_policy"] = legal["image_policy"]
    block["disclaimer"] = legal["disclaimer"]
    return block
