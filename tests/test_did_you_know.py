import copy
import json
from datetime import date

import pytest
from jsonschema import Draft202012Validator, ValidationError

import deduplicator as dd
import fun_fact_extractor as ffe
import pipeline
import validate_data
from adapters import load_adapter
from adapters.base_adapter import make_item
from conftest import ROOT, load_fixture
from http_client import FetchError

DYK_URL = "api.wikimedia.org/feed/v1/wikipedia/en/featured"


def dyk_source():
    config = json.loads((ROOT / "sources" / "sources.json").read_text())
    return next(s for s in config["sources"] if s["id"] == "wikimedia_dyk")


def event(year, title, summary=None, category="events", featured=False, image=None, provider="wikimedia"):
    return make_item(year=year, category=category, title=title, summary=summary or title, provider=provider,
                     image_url=image, is_featured=featured, source_url="https://en.wikipedia.org/wiki/X")


def sample_items():
    return [
        event(1963, "Doctor Who", "The first episode of Doctor Who is broadcast, a record audience tunes in.", featured=True),
        event(1801, "Vaccination", "The first accidental discovery of a smallpox vaccine is recorded in the town."),
        event(1888, "Blizzard", "A record snowfall buries the city and the unusual storm is banned from forecasts."),
        event(1912, "Umbrella", "An inventor patents the umbrella he invented after a rainy picnic in London."),
        event(1700, "Plain", "A treaty was signed between two neighbouring kingdoms after a long negotiation."),
        event(1990, "Dahl", "Roald Dahl, British author, dies in Oxford.", category="deaths"),
    ]


# ---- adapter ---------------------------------------------------------------
def test_dyk_adapter_extracts_clean_hooks_and_article_links():
    payload = load_fixture("wikimedia_dyk_11-24.json")
    Draft202012Validator(json.loads((ROOT / "schema/wikimedia_dyk/response.schema.json").read_text())).validate(payload)
    facts = load_adapter(dyk_source()).transform(payload, 11, 24)

    assert len(facts) == 3  # the too-short hook is dropped
    first = facts[0]
    assert first["fact"].startswith("...that during construction") and first["fact"].endswith("praise\"?")
    assert "<" not in first["fact"] and 40 <= len(first["fact"]) <= 240
    assert first["source_url"] == "https://en.wikipedia.org/wiki/First_Christian_Church_(Walker_Avenue,_Oklahoma_City)"
    assert first["page_title"] == "First Christian Church (Walker Avenue, Oklahoma City)"
    assert first["providers"] == ["wikimedia_dyk"] and first["image_url"] is None and first["image_credit"] is None
    assert facts[1]["source_url"] == "https://en.wikipedia.org/wiki/Mari,_Syria"
    assert facts[2]["source_url"] is None


def test_dyk_adapter_degrades_when_feed_has_no_dyk():
    adapter = load_adapter(dyk_source())
    assert adapter.transform({"tfa": {"title": "X"}}, 11, 23) == []
    assert adapter.transform({"dyk": None, "tfa": {}}, 11, 23) == []


def test_dyk_input_schema_rejects_html_error_page():
    validator = Draft202012Validator(json.loads((ROOT / "schema/wikimedia_dyk/response.schema.json").read_text()))
    with pytest.raises(ValidationError):
        validator.validate({"type": "https://mediawiki.org/wiki/HyperSwitch/errors/not_found", "title": "Not found."})


# ---- extractor -------------------------------------------------------------
def test_fact_id_is_deterministic_and_text_based():
    a = ffe.fact_id(11, 24, "On November 24, 1971: D. B. Cooper hijacks a plane and vanishes into the night.")
    assert a == ffe.fact_id(11, 24, "on november 24, 1971 d b cooper hijacks a plane and vanishes into the night")
    assert a.startswith("dyk-11-24-") and len(a) == len("dyk-11-24-") + 8
    assert a != ffe.fact_id(11, 25, "On November 24, 1971: D. B. Cooper hijacks a plane and vanishes into the night.")


def test_candidates_are_deterministic_non_featured_and_valid():
    items = sample_items()
    first = ffe.candidates(items, 11, 23)
    assert first == ffe.candidates(list(reversed(items)), 11, 23)
    assert first and all(40 <= len(c["fact"]) <= 240 for c in first)
    assert not any("Doctor Who" in c["fact"] for c in first)  # featured item excluded
    assert not any("Roald Dahl" in c["fact"] for c in first)  # deaths are never fun facts
    assert first[0]["fact"].startswith("On November 23, ")
    assert ffe.infer_tag("The first rocket reaches orbit around the Moon") == "Space"
    assert ffe.fit("word " * 100).endswith("\u2026") and len(ffe.fit("word " * 100)) <= 240


def test_candidates_skip_religious_feast_days():
    holidays = [event(0, "Feast", "Christian feast day: Saint Clement of Rome and the martyrs", category="holidays"),
                event(0, "Thanksgiving", "Thanksgiving Day is observed with a big family dinner in the United States.", category="holidays")]
    facts = ffe.candidates(holidays, 11, 26)
    assert len(facts) == 1 and "Thanksgiving" in facts[0]["fact"] and facts[0]["tag"] == "Culture"


# ---- build_did_you_know ----------------------------------------------------
def feed_fact(text, **extra):
    return {"fact": text, "tag": "History Trivia", "image_url": None, "source_url": None,
            "providers": ["wikimedia_dyk"], **extra}


def test_build_uses_feed_first_then_tops_up_with_heuristics():
    feed = [feed_fact("...that an estimated 20,000 tablets and fragments were unearthed at Mari in 1934?")]
    facts = dd.build_did_you_know(sample_items(), 11, 23, feed_facts=feed)
    assert 3 <= len(facts) <= 5
    assert facts[0]["providers"] == ["wikimedia_dyk"] and facts[0]["id"].startswith("dyk-11-23-")
    assert len({f["id"] for f in facts}) == len(facts)
    assert facts == dd.build_did_you_know(sample_items(), 11, 23, feed_facts=copy.deepcopy(feed))


def test_build_caps_at_max_and_skips_featured_duplicates():
    feed = [feed_fact(f"...that fact number {i} about something entirely different happened here?") for i in range(8)]
    assert len(dd.build_did_you_know(sample_items(), 11, 23, feed_facts=feed)) == 5
    dup = [feed_fact("On November 23, 1963: The first episode of Doctor Who is broadcast, a record audience tunes in.")]
    facts = dd.build_did_you_know(sample_items(), 11, 23, feed_facts=dup)
    assert all("Doctor Who" not in f["fact"] for f in facts)


def test_build_retains_previous_feed_facts_when_feed_is_empty():
    retained = [{**feed_fact("...that an estimated 20,000 tablets and fragments were unearthed at Mari in 1934?"),
                 "id": "dyk-11-23-deadbeef"}]
    facts = dd.build_did_you_know(sample_items(), 11, 23, feed_facts=[], retained_facts=retained)
    assert facts[0]["providers"] == ["wikimedia_dyk"] and len(facts) >= 3


# ---- pipeline --------------------------------------------------------------
@pytest.fixture
def dyk_pipeline(tmp_path, monkeypatch):
    def factory(responses, today=date(2026, 11, 24)):
        def fake_get_json(url, *, user_agent, params=None):
            for needle, response in responses.items():
                if needle in url:
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise FetchError(f"no fixture for {url}")

        monkeypatch.setattr(pipeline, "get_json", fake_get_json)
        p = pipeline.build_pipeline(ROOT, data_dir=tmp_path, only_sources=["wikimedia"], resolve_images=False)
        p.sleep = lambda _: None
        p.today = today
        return p

    return factory


def write_day(tmp_path, key="11-24", with_facts=False):
    month, day = key.split("-")
    items = []
    summaries = [
        "A famous opera house reopens to the public after a long restoration.",
        "The parliament approves a new national flag following months of debate.",
        "Engineers complete the longest suspension bridge across the wide estuary.",
        "A chemist accidentally discovered a new dye while cleaning a laboratory flask.",
        "A baker invented a record-sized loaf, and the unusual feat was banned from contests.",
        "Explorers reach the first summit of a remote mountain after a surprising detour.",
    ]
    for i, summary in enumerate(summaries):
        year = 1900 + i
        text = f"Thing {i}"
        items.append({"id": f"{key}-{year}-0000000{i}", "year": year, "year_display": str(year), "category": "events",
                      "title": text, "summary": summary, "image_url": None,
                      "source_url": "https://en.wikipedia.org/wiki/X", "is_featured": i < 3, "providers": ["wikimedia"]})
    doc = {"date": key, "display_date": f"November {int(day)}", "last_updated": "2025-01-01T00:00:00Z",
           "sources_used": ["wikimedia"], "items": items}
    if with_facts:
        doc["did_you_know"] = []
    (tmp_path / f"{key}.json").write_text(json.dumps(doc))


def test_dyk_day_adds_feed_and_heuristic_facts_and_validates(dyk_pipeline, tmp_path):
    p = dyk_pipeline({DYK_URL: load_fixture("wikimedia_dyk_11-24.json")})
    write_day(tmp_path)
    assert p.dyk_day(11, 24) == "dyk-added"
    doc = json.loads((tmp_path / "11-24.json").read_text())
    facts = doc["did_you_know"]
    assert 3 <= len(facts) <= 5 and "wikimedia_dyk" in doc["sources_used"]
    assert facts[0]["fact"].startswith("...that") and facts[0]["providers"] == ["wikimedia_dyk"]
    assert list(doc).index("did_you_know") < list(doc).index("items")
    assert all("image_credit" not in f for f in facts)  # no image, so no optional credit key
    assert validate_data.validate_file(tmp_path / "11-24.json", p.schema_validator) == []
    assert p.dyk_day(11, 24) == "unchanged"


def test_dyk_day_outside_feed_window_uses_heuristics_only(dyk_pipeline, tmp_path):
    p = dyk_pipeline({}, today=date(2026, 6, 1))  # no fixture: any feed request would raise FetchError
    write_day(tmp_path)
    assert p.dyk_day(11, 24) == "dyk-added"
    facts = json.loads((tmp_path / "11-24.json").read_text())["did_you_know"]
    assert len(facts) >= 3 and all(f["providers"] == ["wikimedia"] for f in facts)
    assert p.dyk_day(11, 24) == "dyk-present"
    again = pipeline.build_pipeline(ROOT, data_dir=tmp_path, only_sources=["wikimedia"], resolve_images=False)
    again.today = date(2026, 6, 1)
    assert again.dyk_day(11, 24, force=True) == "unchanged"  # deterministic: same input, same file


def test_dyk_feed_failure_keeps_heuristic_facts_and_retains_old_feed_facts(dyk_pipeline, tmp_path):
    write_day(tmp_path)
    p = dyk_pipeline({DYK_URL: load_fixture("wikimedia_dyk_11-24.json")})
    p.dyk_day(11, 24)
    before = [f for f in json.loads((tmp_path / "11-24.json").read_text())["did_you_know"] if "wikimedia_dyk" in f["providers"]]
    broken = dyk_pipeline({DYK_URL: FetchError("HTTP 500")})
    broken.dyk_day(11, 24, force=True)
    after = json.loads((tmp_path / "11-24.json").read_text())["did_you_know"]
    assert [f for f in after if "wikimedia_dyk" in f["providers"]] == before
    assert 3 <= len(after) <= 5


# ---- schema / validator ----------------------------------------------------
def schema_validator():
    return Draft202012Validator(json.loads((ROOT / "schema/higle/higle-day.schema.json").read_text()))


def test_schema_keeps_did_you_know_optional_and_strict(dyk_pipeline, tmp_path):
    write_day(tmp_path)
    doc = json.loads((tmp_path / "11-24.json").read_text())
    schema_validator().validate(doc)  # legacy files without did_you_know still validate
    fact = {"id": "dyk-11-24-a1b2c3d4", "fact": "...that a perfectly ordinary and entirely sufficient fact exists?",
            "tag": "Oddities", "image_url": None, "source_url": None}
    schema_validator().validate({**doc, "did_you_know": [fact]})
    for bad in ({**fact, "id": "dyk-1-24-xyz"}, {**fact, "fact": "too short"}, {**fact, "fact": "<b>" + fact["fact"]},
                {**fact, "extra": 1}, {k: v for k, v in fact.items() if k != "tag"}):
        with pytest.raises(ValidationError):
            schema_validator().validate({**doc, "did_you_know": [bad]})
    with pytest.raises(ValidationError):
        schema_validator().validate({**doc, "did_you_know": [fact] * 6})


def test_validator_flags_bad_facts(dyk_pipeline, tmp_path):
    p = dyk_pipeline({}, today=date(2026, 6, 1))
    write_day(tmp_path)
    assert any("missing did_you_know" in e for e in validate_data.validate_file(tmp_path / "11-24.json", p.schema_validator))
    p.dyk_day(11, 24)
    path = tmp_path / "11-24.json"
    doc = json.loads(path.read_text())
    doc["did_you_know"][0]["image_url"] = "https://example.com/x.jpg"
    doc["did_you_know"][1]["id"] = "dyk-11-25-aaaaaaaa"
    path.write_text(json.dumps(doc))
    errors = validate_data.validate_file(path, p.schema_validator)
    assert any("must be a Commons file" in e for e in errors) and any("does not match date" in e for e in errors)
    doc["did_you_know"] = doc["did_you_know"][:2]
    path.write_text(json.dumps(doc))
    assert any("expected 3-5" in e for e in validate_data.validate_file(path, p.schema_validator))
