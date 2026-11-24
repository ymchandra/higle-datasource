"""Wikimedia featured feed "Did you know" adapter (https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{YYYY}/{MM}/{DD}).

Only the ``dyk`` array is used. The feed currently has it for today and upcoming dates only (it is absent for past
dates), and entries carry just ``text``/``html`` (the ``pages`` thumbnails some Wikimedia docs mention are not
returned), so the article is taken from the bolded link in ``html``. Missing ``dyk`` simply yields no facts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

from fun_fact_extractor import clean_dyk_text, infer_tag

from .base_adapter import BaseAdapter, format_url, https_url

_BOLD_LINK = re.compile(r"<b\b[^>]*>\s*<a\b[^>]*\bhref=\"([^\"]+)\"", re.IGNORECASE)
_ANY_LINK = re.compile(r"<a\b[^>]*\bhref=\"([^\"]+)\"", re.IGNORECASE)


def article_url(html_text: str | None) -> str | None:
    """The hook's subject article: the first bolded wiki link, else the first wiki link."""
    for pattern in (_BOLD_LINK, _ANY_LINK):
        for href in pattern.findall(html_text or ""):
            url = https_url(href)
            if url and url.startswith("https://en.wikipedia.org/wiki/") and "#" not in url and "?" not in url:
                return url
    return None


def page_title(url: str | None) -> str | None:
    """'https://en.wikipedia.org/wiki/Mari,_Syria' -> 'Mari, Syria'."""
    if not url:
        return None
    return unquote(urlparse(url).path.removeprefix("/wiki/")).replace("_", " ") or None


class WikimediaDykAdapter(BaseAdapter):
    def fetch(self, month: int, day: int, *, user_agent: str, get_json, year: int | None = None) -> Any:
        year = year or datetime.now(timezone.utc).year
        url = format_url(self.source["base_url"], month, day).replace("{YYYY}", f"{year:04d}")
        return get_json(url, user_agent=user_agent)

    def transform(self, payload: Any, month: int, day: int) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for entry in payload.get("dyk") or []:
            fact = clean_dyk_text(entry.get("text") or entry.get("html"))
            if not fact:
                continue
            page = (entry.get("pages") or [{}])[0]
            source_url = ((page.get("content_urls") or {}).get("desktop") or {}).get("page") or article_url(
                entry.get("html")
            )
            facts.append(
                {
                    "fact": fact,
                    "tag": infer_tag(fact),
                    "image_url": https_url((page.get("thumbnail") or {}).get("source")),
                    "image_credit": None,
                    "source_url": https_url(source_url),
                    "providers": [self.source_id],
                    "page_title": page_title(https_url(source_url)),
                }
            )
        return facts
