"""dayinhistory.dev adapter (https://api.dayinhistory.dev/v1/{events,births,deaths}/{month}/{day}/).

The API is paginated (DRF style: ``count/next/previous/results``) and expects a full lowercase
month name. ``fetch`` merges all pages of every configured endpoint into one payload
``{"events": [...], "births": [...], "deaths": [...]}`` which is what the schema validates.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from .base_adapter import (
    BaseAdapter,
    format_url,
    https_url,
    make_item,
    parse_year,
    strip_html,
    truncate,
)

log = logging.getLogger(__name__)

_TRUNCATED_BIO_RE = re.compile(r"\b[A-Z]\.$")


def _person_summary(person: dict[str, Any]) -> str:
    name = strip_html(person.get("name"))
    description = strip_html(person.get("description"))
    bio = strip_html(person.get("bio"))
    # Some bios are cut off mid-abbreviation ("... was a U."); only trust complete-looking ones.
    if len(bio) >= 60 and bio.endswith(".") and not _TRUNCATED_BIO_RE.search(bio):
        return bio
    return f"{name}, {description}" if description else name


class DayInHistoryAdapter(BaseAdapter):
    def fetch(self, month: int, day: int, *, user_agent: str, get_json) -> Any:
        endpoints = {"events": self.source["base_url"], **(self.source.get("extra_endpoints") or {})}
        page_size = self.source.get("page_size", 100)
        max_pages = self.source.get("max_pages", 1)
        delay = self.source.get("rate_limit_delay_sec", 0)
        payload: dict[str, list] = {}

        for index, (key, template) in enumerate(endpoints.items()):
            url: str | None = format_url(template, month, day)
            params: dict[str, Any] | None = {"page_size": page_size}
            results: list = []
            try:
                for page_number in range(max_pages):
                    if page_number or index:
                        time.sleep(delay)
                    page = get_json(url, user_agent=user_agent, params=params)
                    if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                        raise ValueError(f"unexpected dayinhistory page shape for {key}")
                    results.extend(page["results"])
                    url, params = https_url(page.get("next")), None
                    if not url:
                        break
            except Exception as exc:
                if key == "events":
                    raise
                log.warning("dayinhistory %s endpoint failed (%s); continuing", key, exc)
                continue
            payload[key] = results
        return payload

    def transform(self, payload: Any, month: int, day: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for entry in payload.get("events") or []:
            year = parse_year(entry.get("year"))
            title = strip_html(entry.get("title"))
            if year is None or not title:
                continue
            description = strip_html(entry.get("description"))
            items.append(
                make_item(
                    year=year,
                    category="events",
                    title=truncate(title),
                    summary=description or title,
                    provider=self.source_id,
                )
            )
        for key, category, year_field in (
            ("births", "births", "birth_year"),
            ("deaths", "deaths", "death_year"),
        ):
            for person in payload.get(key) or []:
                year = parse_year(person.get(year_field))
                name = strip_html(person.get("name"))
                if year is None or not name:
                    continue
                items.append(
                    make_item(
                        year=year,
                        category=category,
                        title=truncate(name),
                        summary=_person_summary(person),
                        provider=self.source_id,
                        image_url=person.get("image"),
                    )
                )
        return items
