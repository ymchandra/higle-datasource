"""Shared adapter interface and text/year/id helpers."""

from __future__ import annotations

import calendar
import hashlib
import html
import re
from typing import Any

CATEGORIES = ("events", "births", "deaths", "holidays")
MAX_TITLE_LENGTH = 140

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"^\s*(?:(AD|CE)\s*)?(-?\d+)\s*(BCE|BC|AD|CE)?\s*$", re.IGNORECASE)


def strip_html(text: str | None) -> str:
    """Remove HTML tags/entities and collapse whitespace into plain text."""
    if not text:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", text))).strip()


def normalize_title(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace (used for ids)."""
    cleaned = re.sub(r"[\W_]+", " ", text.lower())
    return _WS_RE.sub(" ", cleaned).strip()


def make_id(month: int, day: int, year: int, title: str) -> str:
    digest = hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:8]
    return f"{month:02d}-{day:02d}-{year}-{digest}"


def parse_year(value: Any) -> int | None:
    """Parse 1963, "1963", "534 BC", "44 bce", "-44" into an int (BCE negative)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    match = _YEAR_RE.match(str(value))
    if not match:
        return None
    number = int(match.group(2))
    era = (match.group(3) or "").upper()
    if era in ("BC", "BCE"):
        return -abs(number)
    return number


def format_year(year: int) -> str:
    if year < 0:
        return f"{abs(year)} BCE"
    if year == 0:
        return ""
    return str(year)


def truncate(text: str, limit: int = MAX_TITLE_LENGTH) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: limit - 1]) + "…"


HEADLINE_TARGET = 100


def headline(text: str, limit: int = MAX_TITLE_LENGTH) -> str:
    """Concise headline for sources that only give one long line: cut at a word boundary."""
    text = strip_html(text).rstrip(".")
    return truncate(text, min(limit, HEADLINE_TARGET))


def person_name(text: str, limit: int = MAX_TITLE_LENGTH) -> str:
    """'Tino Anjorin, English footballer' -> 'Tino Anjorin'."""
    text = strip_html(text)
    return truncate(text.split(",", 1)[0] or text, limit)


def https_url(url: Any) -> str | None:
    """Normalize to an https URL (or None); drops Wikimedia's utm_* tracking query to save space."""
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    base, _, query = url.partition("?")
    if query.startswith("utm_source="):
        url = base
    return url if url.startswith("https://") else None


def format_url(template: str, month: int, day: int) -> str:
    """Fill {MM} {DD} (zero-padded), {M} {D} and {month_name} placeholders."""
    return (
        template.replace("{MM}", f"{month:02d}")
        .replace("{DD}", f"{day:02d}")
        .replace("{M}", str(month))
        .replace("{D}", str(day))
        .replace("{month_name}", calendar.month_name[month].lower())
    )


def make_item(
    *,
    year: int,
    category: str,
    title: str,
    summary: str,
    provider: str,
    image_url: str | None = None,
    source_url: str | None = None,
    is_featured: bool = False,
) -> dict[str, Any]:
    """Event in the Higle shape minus ``id`` (assigned after deduplication)."""
    return {
        "year": year,
        "year_display": format_year(year),
        "category": category,
        "title": title,
        "summary": summary,
        "image_url": https_url(image_url),
        "source_url": source_url if isinstance(source_url, str) and source_url.startswith(("http://", "https://")) else None,
        "is_featured": is_featured,
        "providers": [provider],
    }


class BaseAdapter:
    """Fetches one source and transforms its raw payload into Higle items."""

    def __init__(self, source: dict[str, Any]):
        self.source = source
        self.source_id: str = source["id"]

    def fetch(self, month: int, day: int, *, user_agent: str, get_json) -> Any:
        """Return the raw payload for a day (validated against the source schema later)."""
        url = format_url(self.source["base_url"], month, day)
        return get_json(url, user_agent=user_agent)

    def transform(self, payload: Any, month: int, day: int) -> list[dict[str, Any]]:
        raise NotImplementedError
