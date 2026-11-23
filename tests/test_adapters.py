import pytest
from jsonschema import Draft202012Validator, ValidationError

from adapters import load_adapter
from adapters.base_adapter import format_url, format_year, parse_year, strip_html
from conftest import ROOT, load_fixture
from http_client import FetchError, InvalidResponseError, RateLimitedError, get_json

import json


def source(source_id):
    config = json.loads((ROOT / "sources" / "sources.json").read_text())
    return next(s for s in config["sources"] if s["id"] == source_id)


def validator(source_id):
    path = ROOT / source(source_id)["schema_path"]
    return Draft202012Validator(json.loads(path.read_text()))


def by_title(items, text):
    return next(i for i in items if text in i["title"])


def test_helpers():
    assert parse_year("534 BC") == -534
    assert parse_year("44 bce") == -44
    assert parse_year("1963") == 1963
    assert parse_year("Cecilia[35]") is None
    assert format_year(-44) == "44 BCE" and format_year(1963) == "1963"
    assert strip_html("a <b>bold</b> &amp; <a href='x'>link</a>\n text") == "a bold & link text"
    assert format_url("https://x/{MM}/{DD}/{M}/{D}/{month_name}", 3, 7) == "https://x/03/07/3/7/march"


def test_wikimedia_adapter():
    payload = load_fixture("wikimedia_11-23.json")
    validator("wikimedia").validate(payload)
    items = load_adapter(source("wikimedia")).transform(payload, 11, 23)

    # 1 selected + 2 events with a year + birth + death + holiday; the year-less event is dropped.
    assert len(items) == 6
    selected = items[0]
    assert selected["is_featured"] and selected["category"] == "events"
    assert selected["image_url"].endswith("330px-doctor_who.jpg")  # utm tracking query stripped
    assert selected["source_url"] == "https://en.wikipedia.org/wiki/Doctor_Who"
    assert selected["providers"] == ["wikimedia"]

    event = items[1]
    assert "<i>" not in event["summary"] and "Doctor Who" in event["summary"]
    bce = by_title(items, "Thespis")
    assert bce["year"] == -534 and bce["year_display"] == "534 BCE"

    birth = by_title(items, "Miley Cyrus")
    assert birth["category"] == "births" and birth["title"] == "Miley Cyrus"
    assert birth["image_url"] == "https://upload.wikimedia.org/wikipedia/commons/miley.jpg"  # http -> https
    assert by_title(items, "Roald Dahl")["category"] == "deaths"

    holiday = by_title(items, "Christian feast day")
    assert holiday["category"] == "holidays" and holiday["year"] == 0 and holiday["year_display"] == ""
    assert "\n" not in holiday["summary"]


def test_muffinlabs_adapter():
    payload = load_fixture("muffinlabs_11-23.json")
    validator("muffinlabs").validate(payload)
    items = load_adapter(source("muffinlabs")).transform(payload, 11, 23)

    # Holidays upstream are mis-parsed (null text) and must not leak through.
    assert {i["category"] for i in items} == {"events", "births", "deaths"}
    assert by_title(items, "Thespis")["year"] == -534
    assert by_title(items, "Doctor Who")["source_url"] == "https://en.wikipedia.org/wiki/Doctor_Who"
    assert by_title(items, "coin-operated")["source_url"] is None
    assert all(i["providers"] == ["muffinlabs"] and i["image_url"] is None for i in items)


def test_dayinhistory_adapter():
    payload = load_fixture("dayinhistory_11-23.json")
    validator("dayinhistory").validate(payload)
    items = load_adapter(source("dayinhistory")).transform(payload, 11, 23)

    events = [i for i in items if i["category"] == "events"]
    assert len(events) == 2  # unparseable year skipped
    assert by_title(events, "Debut")["summary"].startswith("The first episode")
    bce = by_title(events, "BCE")
    assert bce["year"] == -44 and bce["summary"] == "Example BCE event"  # null description -> title

    birth = by_title(items, "Aaron Bank")
    assert birth["year"] == 1902 and birth["image_url"].startswith("https://cdn.dayinhistory.dev/")
    assert birth["summary"] == "Aaron Bank, United States Army officer"  # truncated bio ignored
    death = by_title(items, "Al Raby")
    assert death["year"] == 1988 and death["summary"].startswith("Al Raby was an African American")


def test_dayinhistory_fetch_paginates_and_tolerates_extra_endpoint_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    cfg = {
        **source("dayinhistory"),
        "max_pages": 2,
        "extra_endpoints": {"births": "https://api.example/births/{month_name}/{D}/"},
    }
    calls = []

    def fake_get_json(url, *, user_agent, params=None):
        calls.append((url, params))
        if "births" in url:
            raise RateLimitedError("429")
        if "page=2" in url:
            return {"next": None, "results": [{"year": "1900", "title": "B"}]}
        return {"next": "http://api.example/events/page=2", "results": [{"year": "1901", "title": "A"}]}

    payload = load_adapter(cfg).fetch(11, 23, user_agent="ua", get_json=fake_get_json)
    assert [e["title"] for e in payload["events"]] == ["A", "B"]
    assert "births" not in payload
    assert calls[0][0] == "https://api.dayinhistory.dev/v1/events/november/23/"
    assert calls[1][0].startswith("https://")  # http `next` links are upgraded


def test_html_error_page_fails_schema():
    for source_id in ("wikimedia", "dayinhistory", "muffinlabs"):
        with pytest.raises(ValidationError):
            validator(source_id).validate("<html>502 Bad Gateway</html>")
        with pytest.raises(ValidationError):
            validator(source_id).validate({"unexpected": "shape"})


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_get_json_retries_with_exponential_backoff():
    sleeps = []
    session = FakeSession(FakeResponse(429), FakeResponse(503), FakeResponse(500), FakeResponse(200, {"ok": 1}))
    assert get_json("u", user_agent="ua", session=session, sleep=sleeps.append) == {"ok": 1}
    assert sleeps == [1.0, 2.0, 4.0] and session.calls == 4


def test_get_json_failure_modes():
    with pytest.raises(RateLimitedError):
        get_json("u", user_agent="ua", session=FakeSession(*[FakeResponse(429)] * 4), sleep=lambda _: None)
    with pytest.raises(FetchError):
        get_json("u", user_agent="ua", session=FakeSession(*[FakeResponse(502)] * 4), sleep=lambda _: None)
    session = FakeSession(FakeResponse(404))
    with pytest.raises(FetchError):
        get_json("u", user_agent="ua", session=session, sleep=lambda _: None)
    assert session.calls == 1  # 404 is not retried
    with pytest.raises(InvalidResponseError):
        get_json("u", user_agent="ua", session=FakeSession(FakeResponse(200)), sleep=lambda _: None)
