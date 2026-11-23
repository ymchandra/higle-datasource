"""Wikimedia On This Day adapter (https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday)."""

from __future__ import annotations

from typing import Any

from .base_adapter import BaseAdapter, headline, make_item, person_name, strip_html

# payload key -> (Higle category, featured)
_SECTIONS = {
    "selected": ("events", True),
    "events": ("events", False),
    "births": ("births", False),
    "deaths": ("deaths", False),
    "holidays": ("holidays", False),
}


class WikimediaAdapter(BaseAdapter):
    def transform(self, payload: Any, month: int, day: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key, (category, featured) in _SECTIONS.items():
            for entry in payload.get(key) or []:
                text = strip_html(entry.get("text"))
                if not text:
                    continue
                year = entry.get("year")
                if year is None:
                    if category != "holidays":
                        continue
                    year = 0
                page = (entry.get("pages") or [{}])[0]
                title = person_name(text) if category in ("births", "deaths") else headline(text)
                items.append(
                    make_item(
                        year=year,
                        category=category,
                        title=title,
                        summary=text,
                        provider=self.source_id,
                        image_url=(page.get("thumbnail") or {}).get("source"),
                        source_url=((page.get("content_urls") or {}).get("desktop") or {}).get("page"),
                        is_featured=featured,
                    )
                )
        return items
