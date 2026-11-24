"""Deterministic "Did you know?" fact helpers: id/format rules and the heuristic fallback extractor.

The Wikimedia ``dyk`` feed only has data for current/upcoming dates, so most of the 366 calendar files rely
on ``extract`` here, which promotes curious, light-hearted entries from the day's merged items. Same input
always gives the same output (no randomness, explicit tie-breaks) so daily diffs stay minimal.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from typing import Any

from adapters.base_adapter import format_year, normalize_title, strip_html

MIN_FACT_LENGTH = 40
MAX_FACT_LENGTH = 240
MAX_TAG_LENGTH = 30
DEFAULT_TAG = "History Trivia"

# keyword -> weight; matched on word boundaries, each distinct keyword counts once.
CURIOSITY_WEIGHTS: dict[str, int] = {
    "invented": 3, "invention": 2, "accidental": 3, "accidentally": 3, "banned": 3, "hoax": 3, "unusual": 3,
    "mysterious": 3, "mystery": 2, "bizarre": 3, "prank": 3, "hijacked": 3, "stolen": 3, "patented": 3,
    "disappears": 3, "disappeared": 3, "vanishes": 3, "escapes": 2, "record": 2, "discovered": 2, "discovers": 2,
    "oldest": 2, "largest": 2, "smallest": 2, "longest": 2, "tallest": 2, "fastest": 2, "first": 2,
    "world's": 2, "unexpected": 2, "strange": 2, "surprising": 2, "legendary": 1, "nicknamed": 1,
}
# grim topics are not "fun"; each distinct match subtracts from the score.
SOMBRE_WORDS = frozenset(
    "killed kills killing massacre massacres genocide murdered murder executed execution assassinated "
    "assassination bombing bombed war invasion invades attack attacks died dies dead deaths crash crashes "
    "famine terrorist terrorism hostage hostages shot slaughter".split()
)
SOMBRE_PENALTY = 3

TAG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (tag, re.compile(pattern, re.IGNORECASE))
    for tag, pattern in (
        ("Mystery & Crime", r"\b(hijack\w*|stol[ea]n?|theft|heist|robber\w*|kidnap\w*|hoax|mysteri\w*|disappear\w*|vanish\w*|escape\w*|fugitive)\b"),
        ("Space", r"\b(moon|lunar|space\w*|satellite|astronaut\w*|cosmonaut\w*|rocket|planet\w*|comet|asteroid|nasa|apollo|orbit\w*|mars|probe)\b"),
        ("Inventions", r"\b(invent\w*|patent\w*|prototype|unveil\w*|demonstrat\w*|introduc\w*|launch\w*)\b"),
        ("Science", r"\b(discover\w*|scientist\w*|physic\w*|chemi\w*|biolog\w*|species|vaccine|theory|experiment\w*|dna|evolution|astronom\w*|telescope|fossil\w*|disease)\b"),
        ("Nature", r"\b(earthquake|volcan\w*|eruption|hurricane|typhoon|tsunami|storm|flood\w*|meteor\w*|eclipse)\b"),
        ("Sports", r"\b(olympic\w*|world cup|championship|football|baseball|cricket|tennis|marathon|tournament|athlete\w*|league|match)\b"),
        ("Culture", r"\b(film|album|novel|book|premiere\w*|song|band|opera|theat\w+|museum|painting|festival|television|broadcast\w*|published|concert|comic)\b"),
        ("Oddities", r"\b(record|oldest|largest|smallest|longest|tallest|fastest|unusual|bizarre|strange|accident\w*|prank|banned|world's)\b"),
    )
)

_SENTENCE_END = re.compile(r"(?<=[a-z0-9\)\"'])\.\s+(?=[A-Z])")
_LEADING_DOTS = re.compile(r"^[\s.\u2026]+")
_DYK_PREFIX = re.compile(r"^(?:did you know\s*)?\.*\s*\u2026?\s*that\s+", re.IGNORECASE)
_BORING_HOLIDAY = re.compile(r"\b(feast days?|christian|saints?|martyrs?)\b", re.IGNORECASE)


def fact_id(month: int, day: int, fact: str) -> str:
    """``dyk-{MM}-{DD}-{sha1(normalized fact)[:8]}``: stable as long as the fact text is."""
    digest = hashlib.sha1(normalize_title(fact).encode("utf-8")).hexdigest()[:8]
    return f"dyk-{month:02d}-{day:02d}-{digest}"


def fit(text: str, limit: int = MAX_FACT_LENGTH) -> str:
    """Trim ``text`` to ``limit`` characters at a word boundary, adding an ellipsis when it was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[: limit - 1]) + "\u2026"


def valid_fact(fact: str) -> bool:
    return MIN_FACT_LENGTH <= len(fact) <= MAX_FACT_LENGTH and "<" not in fact and ">" not in fact


def clean_dyk_text(text: str | None) -> str | None:
    """Wikipedia hook ('... that X?') -> '...that X?'. None when it is not a usable 40-240 char fact."""
    plain = strip_html(text)
    if not plain:
        return None
    body = _DYK_PREFIX.sub("", plain)
    if body == plain:
        body = _LEADING_DOTS.sub("", plain)
    body = body.strip()
    if not body:
        return None
    if not body.endswith("?"):
        body = body.rstrip(".!") + "?"
    fact = f"...that {body}" if _DYK_PREFIX.match(plain) else body
    return fact if valid_fact(fact) else None


def infer_tag(text: str, category: str = "events") -> str:
    if category == "holidays":
        return "Culture"
    for tag, pattern in TAG_RULES:
        if pattern.search(text):
            return tag
    return DEFAULT_TAG


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))


def curiosity_score(item: dict[str, Any]) -> int:
    """Higher = more 'fun fact'. Grim topics subtract; holidays get a small bonus for being quirky."""
    words = _words(item["summary"])
    score = sum(weight for word, weight in CURIOSITY_WEIGHTS.items() if word in words)
    score = min(score, 6) - SOMBRE_PENALTY * min(len(words & SOMBRE_WORDS), 2)
    if item["category"] == "holidays":
        score += 1
    return score


def _first_sentences(summary: str, limit: int) -> str:
    """Whole summary when it fits, else the leading sentence(s) that fit."""
    if len(summary) <= limit:
        return summary
    sentences = _SENTENCE_END.split(summary)
    out = sentences[0]
    for extra in sentences[1:]:
        if len(out) + len(extra) + 2 > limit:
            break
        out += ". " + extra
    return out if len(out) <= limit else summary


def format_fact(item: dict[str, Any], month: int, day: int) -> tuple[str, bool]:
    """Plain-text fact for an event/holiday item and whether it had to be shortened."""
    summary = strip_html(item["summary"]).strip()
    if item["category"] == "holidays" or item["year"] == 0:
        head = f"{calendar.month_name[month]} {day} is observed as "
        body = fit(summary.rstrip("."), MAX_FACT_LENGTH - len(head) - 1)
        return f"{head}{body}.", body != summary.rstrip(".")
    head = f"On {calendar.month_name[month]} {day}, {format_year(item['year'])}: "
    room = MAX_FACT_LENGTH - len(head)
    body = _first_sentences(summary.rstrip("."), room)
    trimmed = fit(body, room - 1) if len(body) > room - 1 else body
    shortened = trimmed != summary.rstrip(".")
    return f"{head}{trimmed}.", shortened


def candidates(items: list[dict[str, Any]], month: int, day: int) -> list[dict[str, Any]]:
    """Ranked fact candidates (best first) built from non-featured events/holidays; deterministic."""
    ranked: list[tuple[tuple, dict[str, Any]]] = []
    for item in items:
        if item["is_featured"] or item["category"] not in ("events", "holidays"):
            continue
        if item["category"] == "holidays" and _BORING_HOLIDAY.search(item["title"] + " " + item["summary"]):
            continue
        fact, shortened = format_fact(item, month, day)
        if not valid_fact(fact):
            continue
        score = curiosity_score(item) - (2 if shortened else 0) + (1 if item.get("image_url") else 0)
        ranked.append(
            (
                (-score, item["category"] == "holidays", -item["year"], item.get("id", item["title"])),
                {
                    "fact": fact,
                    "tag": infer_tag(item["summary"], item["category"]),
                    "image_url": item.get("image_url"),
                    "image_credit": item.get("image_credit"),
                    "source_url": item.get("source_url"),
                    "providers": list(item["providers"]),
                    "score": score,
                },
            )
        )
    ranked.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in ranked]


def diversified_order(ranked: list[dict[str, Any]], per_tag: int = 2) -> list[dict[str, Any]]:
    """Rank order with at most ``per_tag`` candidates per tag first and the overflow after them."""
    counts: dict[str, int] = {}
    first: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for candidate in ranked:
        counts[candidate["tag"]] = counts.get(candidate["tag"], 0) + 1
        (first if counts[candidate["tag"]] <= per_tag else overflow).append(candidate)
    return first + overflow
