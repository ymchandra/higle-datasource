"""Muffin Labs adapter (https://history.muffinlabs.com/date/{M}/{D})."""

from __future__ import annotations

from typing import Any

from .base_adapter import BaseAdapter, headline, make_item, parse_year, person_name, strip_html

# "Holidays and observances" is deliberately skipped: upstream returns mis-parsed rows
# (null text, the holiday name in the year field), and Wikimedia already provides holidays.
_SECTIONS = {
    "Events": "events",
    "Births": "births",
    "Deaths": "deaths",
}


class MuffinLabsAdapter(BaseAdapter):
    def transform(self, payload: Any, month: int, day: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        data = payload.get("data") or {}
        for key, category in _SECTIONS.items():
            for entry in data.get(key) or []:
                text = strip_html(entry.get("text"))
                if not text:
                    continue
                year = parse_year(entry.get("year"))
                if year is None:
                    if category != "holidays":
                        continue
                    year = 0
                links = entry.get("links") or []
                title = person_name(text) if category in ("births", "deaths") else headline(text)
                items.append(
                    make_item(
                        year=year,
                        category=category,
                        title=title,
                        summary=text,
                        provider=self.source_id,
                        source_url=links[0].get("link") if links else None,
                    )
                )
        return items
