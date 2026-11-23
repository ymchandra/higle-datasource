"""Image licence policy: keep only freely licensed Wikimedia Commons images and record their credit.

Event images are *hotlinked* (never copied into this repository), but each file has its own licence.
Wikipedia also hosts non-free "fair use" images locally (``/wikipedia/en/...``); those must not be
redistributed. The policy therefore keeps an ``image_url`` only when

* it points at ``upload.wikimedia.org`` / ``thumb.wikimedia.org`` under ``/wikipedia/commons/``
  (Commons accepts only free or public-domain files), and
* the Commons API reports a licence of Public domain, CC0, CC BY or CC BY-SA and no "non-free" flag.

For kept images ``image_credit`` records author, licence and the Commons file page, so the Higle app can
show proper attribution. Anything else is dropped (``image_url`` becomes null).
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from collections import Counter
from typing import Any, Callable, Iterable

from adapters.base_adapter import strip_html, truncate
from http_client import FetchError, RateLimitedError, get_json

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_FILE_PAGE = "https://commons.wikimedia.org/wiki/"
BATCH_SIZE = 50  # titles per Action API query
DEFAULT_DELAY_SEC = 1.0  # one request at a time, well inside Wikimedia's per-minute limits
MAX_AUTHOR_LENGTH = 120
METADATA_FILTER = "LicenseShortName|LicenseUrl|Artist|Credit|NonFree"

_COMMONS_PATH = re.compile(
    r"^https://(?:upload|thumb)\.wikimedia\.org/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/?#]+)"
)
# Licence short names accepted as free to reuse with attribution: public domain, CC0, CC BY, CC BY-SA.
_ALLOWED_LICENSE = re.compile(r"^(?:public domain|pd(?:[- ]|$)|cc0|cc[- ]zero|cc[- ]by(?:[- ]sa)?[- ]\d)", re.IGNORECASE)


def commons_title(image_url: str | None) -> str | None:
    """'File:Name with spaces.jpg' for a Commons upload/thumbnail URL, else None (not Commons / not Wikimedia)."""
    match = _COMMONS_PATH.match(image_url or "")
    if not match:
        return None
    return "File:" + urllib.parse.unquote(match.group(1)).replace("_", " ")


def _meta(extmetadata: dict[str, Any], key: str) -> str:
    value = (extmetadata.get(key) or {}).get("value")
    return value.strip() if isinstance(value, str) else ""


def classify(page: dict[str, Any]) -> dict[str, Any]:
    """Turn one Commons API page object into ``{"credit": {...}}`` or ``{"rejected": reason}``."""
    if page.get("missing") or not page.get("imageinfo"):
        return {"rejected": "missing"}
    info = page["imageinfo"][0]
    meta = info.get("extmetadata") or {}
    if _meta(meta, "NonFree").lower() in ("true", "1"):
        return {"rejected": "non-free"}
    license_name = _meta(meta, "LicenseShortName")
    if not license_name:
        return {"rejected": "unknown-license"}
    if not _ALLOWED_LICENSE.match(license_name):
        return {"rejected": f"license:{license_name}"}
    author = truncate(strip_html(_meta(meta, "Artist")) or strip_html(_meta(meta, "Credit")), MAX_AUTHOR_LENGTH)
    page_url = (info.get("descriptionurl") or "").split("?")[0] or (
        COMMONS_FILE_PAGE + urllib.parse.quote(page["title"].replace(" ", "_"), safe=":(),")
    )
    return {
        "credit": {
            "author": author or None,
            "license": license_name,
            "license_url": _meta(meta, "LicenseUrl") or None,
            "file_page_url": page_url,
        }
    }


class ImageLicenseResolver:
    """Looks up Commons licence metadata in batches (sequentially, with a delay) and memoizes results."""

    def __init__(
        self,
        *,
        user_agent: str,
        fetch: Callable[..., Any] = get_json,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = DEFAULT_DELAY_SEC,
        batch_size: int = BATCH_SIZE,
        api_url: str = COMMONS_API,
    ):
        self.user_agent = user_agent
        self._fetch = fetch
        self._sleep = sleep
        self.delay = delay
        self.batch_size = batch_size
        self.api_url = api_url
        self.memo: dict[str, dict[str, Any]] = {}
        self.disabled = False

    def lookup(self, titles: Iterable[str]) -> None:
        """Resolve titles not memoized yet. Failed batches stay unresolved (retried on a later call)."""
        pending = [t for t in dict.fromkeys(titles) if t not in self.memo]
        for start in range(0, len(pending), self.batch_size):
            if self.disabled:
                return
            batch = pending[start : start + self.batch_size]
            try:
                payload = self._fetch(
                    self.api_url,
                    user_agent=self.user_agent,
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": 2,
                        "prop": "imageinfo",
                        "iiprop": "extmetadata|url",
                        "iiextmetadatafilter": METADATA_FILTER,
                        "redirects": 1,
                        "titles": "|".join(batch),
                    },
                )
                self._ingest(batch, payload)
            except RateLimitedError as exc:
                log.warning("Commons lookups disabled for this run: %s", exc)
                self.disabled = True
            except (FetchError, KeyError, TypeError, ValueError) as exc:
                log.warning("Commons lookup failed for %d titles: %s", len(batch), exc)
            finally:
                self._sleep(self.delay)

    def _ingest(self, batch: list[str], payload: dict[str, Any]) -> None:
        query = payload["query"]
        alias = {}
        for change in (query.get("normalized") or []) + (query.get("redirects") or []):
            alias[change["from"]] = change["to"]
        pages = {page["title"]: page for page in query.get("pages") or []}
        for title in batch:
            target = title
            for _ in range(3):  # normalized -> redirect chain
                target = alias.get(target, target)
            page = pages.get(target)
            if page is not None:
                self.memo[title] = classify(page)

    def get(self, title: str) -> dict[str, Any] | None:
        return self.memo.get(title)


def apply_image_policy(
    items: list[dict[str, Any]],
    resolver: ImageLicenseResolver | None,
    previous_items: Iterable[dict[str, Any]] = (),
) -> Counter:
    """Set ``image_url``/``image_credit`` on every item in place; returns counts per outcome.

    Outcomes: ``kept``, ``no-image``, ``not-commons`` (host/path not accepted), ``rejected:<reason>``
    and ``unresolved`` (lookup failed and no earlier credit known -> image dropped, retried next run).
    """
    stats: Counter = Counter()
    known: dict[str, dict[str, Any]] = {}
    for old in previous_items:
        title = commons_title(old.get("image_url"))
        if title and old.get("image_credit"):
            known[title] = old["image_credit"]

    titles = {commons_title(i.get("image_url")) for i in items}
    titles.discard(None)
    if resolver and titles:
        resolver.lookup(t for t in sorted(titles) if t not in known)

    for item in items:
        url = item.get("image_url")
        if not url:
            item["image_url"], item["image_credit"] = None, None
            stats["no-image"] += 1
            continue
        title = commons_title(url)
        result = None
        if title is None:
            outcome = "not-commons"
        elif title in known:
            result, outcome = {"credit": known[title]}, "kept"
        elif resolver and (result := resolver.get(title)) is not None:
            outcome = "kept" if "credit" in result else f"rejected:{result['rejected']}"
        else:
            outcome = "unresolved"
        if result and "credit" in result:
            item["image_credit"] = result["credit"]
        else:
            item["image_url"], item["image_credit"] = None, None
        stats[outcome] += 1
    return stats
