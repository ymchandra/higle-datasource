"""Deduplicate, enrich, rank, cap and sort events merged from several sources."""

from __future__ import annotations

import re
from typing import Any, Iterable

from rapidfuzz import fuzz

from adapters.base_adapter import CATEGORIES, make_id

DEFAULT_THRESHOLD = 75
DEFAULT_QUOTAS = {"events": 45, "births": 25, "deaths": 20, "holidays": 10}

STOP_WORDS = frozenset(
    "a an and are as at be by for from had has have he her his in is it its of on or she that the "
    "their they this to was were which who with".split()
)


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and stop words."""
    words = re.sub(r"[\W_]+", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in STOP_WORDS)


def _similar(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    # token_set_ratio is 100 whenever one token set contains the other, so a single
    # token (e.g. "war") would match anything containing it; require an exact match then.
    if len(a.split()) < 2 or len(b.split()) < 2:
        return a == b
    return fuzz.token_set_ratio(a, b) >= threshold


def is_duplicate(a: dict[str, Any], b: dict[str, Any], threshold: float = DEFAULT_THRESHOLD) -> bool:
    """Same year + category, and similar normalized title or summary."""
    if a["year"] != b["year"] or a["category"] != b["category"]:
        return False
    return _similar(normalize_text(a["title"]), normalize_text(b["title"]), threshold) or _similar(
        normalize_text(a["summary"]), normalize_text(b["summary"]), threshold
    )


def _append_unique(target: list[str], values: Iterable[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def merge_items(primary: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Merge ``other`` into a copy of ``primary`` (the higher-priority source)."""
    merged = {**primary, "providers": list(primary["providers"])}
    if len(other["summary"]) > len(merged["summary"]):
        merged["summary"] = other["summary"]
    if not merged.get("image_url"):
        merged["image_url"] = other.get("image_url")
    if not merged.get("source_url"):
        merged["source_url"] = other.get("source_url")
    merged["is_featured"] = bool(merged["is_featured"] or other["is_featured"])
    _append_unique(merged["providers"], other["providers"])
    return merged


def deduplicate(items: list[dict[str, Any]], threshold: float = DEFAULT_THRESHOLD) -> list[dict[str, Any]]:
    """Merge duplicates. ``items`` must be ordered by source priority (best first)."""
    # Each cluster keeps its merged item plus every original member, so a newcomer can match
    # any phrasing seen so far (not just the merged winner).
    groups: dict[tuple[int, str], list[tuple[dict[str, Any], list[dict[str, Any]]]]] = {}
    for item in items:
        clusters = groups.setdefault((item["year"], item["category"]), [])
        for index, (merged, members) in enumerate(clusters):
            if any(is_duplicate(member, item, threshold) for member in members):
                clusters[index] = (merge_items(merged, item), members + [item])
                break
        else:
            clusters.append(({**item, "providers": list(item["providers"])}, [item]))
    return [merged for clusters in groups.values() for merged, _ in clusters]


def score(item: dict[str, Any]) -> tuple:
    """Higher is more notable: confirmed by more sources, has an image, richer summary."""
    return (len(item["providers"]), bool(item.get("image_url")), min(len(item["summary"]), 400))


def ensure_featured(items: list[dict[str, Any]], min_featured: int = 3, max_featured: int | None = 5) -> None:
    """Make the featured flags number between ``min_featured`` and ``max_featured`` (in place)."""
    featured = [i for i in items if i["is_featured"]]
    if max_featured is not None and len(featured) > max_featured:
        keep = {id(i) for i in sorted(featured, key=score, reverse=True)[:max_featured]}
        for item in featured:
            item["is_featured"] = id(item) in keep
        return
    if len(featured) >= min_featured:
        return
    candidates = sorted(
        (i for i in items if not i["is_featured"] and i["category"] == "events"),
        key=lambda i: (bool(i.get("image_url")),) + score(i),
        reverse=True,
    )
    for item in candidates[: min_featured - len(featured)]:
        item["is_featured"] = True


def select_items(
    items: list[dict[str, Any]],
    max_items: int = 100,
    quotas: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Cap to ``max_items``: featured always kept, then per-category quotas, then best leftovers."""
    if len(items) <= max_items:
        return list(items)
    quotas = DEFAULT_QUOTAS if quotas is None else quotas
    ranked = sorted(range(len(items)), key=lambda i: score(items[i]), reverse=True)
    chosen = {i for i in ranked if items[i]["is_featured"]}
    for category in CATEGORIES:
        taken = sum(1 for i in chosen if items[i]["category"] == category)
        for i in ranked:
            if taken >= quotas.get(category, 0) or len(chosen) >= max_items:
                break
            if i not in chosen and items[i]["category"] == category:
                chosen.add(i)
                taken += 1
    for i in ranked:
        if len(chosen) >= max_items:
            break
        chosen.add(i)
    return [items[i] for i in sorted(chosen)]


def sort_items(items: list[dict[str, Any]], order: str = "desc") -> list[dict[str, Any]]:
    """Chronological sort (``desc`` = newest first). Yearless holidays always go last."""
    sign = -1 if order == "desc" else 1
    category_rank = {c: n for n, c in enumerate(CATEGORIES)}
    return sorted(
        items,
        key=lambda i: (i["category"] == "holidays", sign * i["year"], category_rank[i["category"]], i["title"]),
    )


def assign_ids(items: list[dict[str, Any]], month: int, day: int) -> None:
    """Deterministic ids ``MM-DD-year-sha1(title)[:8]``; collisions get a ``-N`` suffix."""
    used: set[str] = set()
    for item in items:
        base = make_id(month, day, item["year"], item["title"])
        candidate, n = base, 1
        while candidate in used:
            n += 1
            candidate = f"{base}-{n}"
        item["id"] = candidate
        used.add(candidate)


def stabilize_ids(
    items: list[dict[str, Any]], previous: list[dict[str, Any]], threshold: float = DEFAULT_THRESHOLD
) -> None:
    """Reuse the id of a matching item from the previous file so bookmarks survive title changes."""
    previous_ids = {p["id"] for p in previous}
    current_ids = {i["id"] for i in items}
    unclaimed = [p for p in previous if p["id"] not in current_ids]
    for item in items:
        if item["id"] in previous_ids:
            continue
        for old in unclaimed:
            if is_duplicate(item, old, threshold):
                current_ids.discard(item["id"])
                item["id"] = old["id"]
                current_ids.add(old["id"])
                unclaimed.remove(old)
                break


def process(
    items: list[dict[str, Any]],
    month: int,
    day: int,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    max_items: int = 100,
    quotas: dict[str, int] | None = None,
    sort_order: str = "desc",
    min_featured: int = 3,
    max_featured: int | None = 5,
) -> list[dict[str, Any]]:
    """Full Step 1-4 flow: dedupe/merge, feature, cap, sort, assign ids."""
    merged = deduplicate(items, threshold)
    ensure_featured(merged, min_featured, max_featured)
    selected = sort_items(select_items(merged, max_items, quotas), sort_order)
    assign_ids(selected, month, day)
    return selected
