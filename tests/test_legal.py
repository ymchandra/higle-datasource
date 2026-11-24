import json

import pytest

import legal
import pipeline
import validate_data
from conftest import ROOT
from http_client import FetchError, RateLimitedError
from image_licenses import ImageLicenseResolver, apply_image_policy, classify, commons_title

THUMB = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Marie_Curie%2C_1920.jpg/330px-Marie_Curie%2C_1920.jpg"
ORIGINAL = "https://upload.wikimedia.org/wikipedia/commons/4/4f/Herzberg%2CGerhard_1952_London.jpg"
LOCAL_EN = "https://upload.wikimedia.org/wikipedia/en/thumb/a/ab/Poster.jpg/330px-Poster.jpg"


def page(title, license_name="CC BY-SA 4.0", **meta):
    extmetadata = {
        "LicenseShortName": {"value": license_name},
        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
        "Artist": {"value": '<a href="//x">Jane <b>Doe</b></a>'},
    }
    extmetadata.update({k: {"value": v} for k, v in meta.items()})
    return {
        "title": title,
        "imageinfo": [{"descriptionurl": "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_") + "?x=1",
                       "extmetadata": extmetadata}],
    }


def test_commons_title():
    assert commons_title(THUMB) == "File:Marie Curie, 1920.jpg"
    assert commons_title(ORIGINAL) == "File:Herzberg,Gerhard 1952 London.jpg"
    assert commons_title(LOCAL_EN) is None  # local (possibly non-free) uploads are never accepted
    assert commons_title("https://example.com/wikipedia/commons/a/ab/x.jpg") is None
    assert commons_title(None) is None


@pytest.mark.parametrize("license_name", ["Public domain", "PD-old-70", "CC0", "CC BY 2.0", "CC BY-SA 3.0 de", "CC BY-SA 4.0"])
def test_free_licenses_are_accepted(license_name):
    credit = classify(page("File:A.jpg", license_name))["credit"]
    assert credit["license"] == license_name and credit["author"] == "Jane Doe"
    assert credit["file_page_url"] == "https://commons.wikimedia.org/wiki/File:A.jpg"  # query dropped


@pytest.mark.parametrize("license_name", ["GFDL", "Attribution", "Copyrighted free use", "CC BY-NC 4.0", "Fair use"])
def test_other_licenses_are_rejected(license_name):
    assert classify(page("File:A.jpg", license_name)) == {"rejected": f"license:{license_name}"}


def test_nonfree_missing_and_unknown_license_are_rejected():
    assert classify(page("File:A.jpg", NonFree="true")) == {"rejected": "non-free"}
    assert classify({"title": "File:A.jpg", "missing": True}) == {"rejected": "missing"}
    assert classify({"title": "File:A.jpg", "imageinfo": [{"extmetadata": {}}]}) == {"rejected": "unknown-license"}


def commons_fetch(pages, calls=None):
    def fetch(url, *, user_agent, params):
        if calls is not None:
            calls.append(params["titles"].split("|"))
        return {"query": {"normalized": [], "redirects": [], "pages": [pages[t] for t in params["titles"].split("|") if t in pages]}}

    return fetch


def make_resolver(fetch, **kw):
    return ImageLicenseResolver(user_agent="test", fetch=fetch, sleep=lambda _: None, **kw)


def item(url):
    return {"image_url": url, "source_url": None}


def test_resolver_batches_and_follows_redirects():
    calls = []

    def fetch(url, *, user_agent, params):
        calls.append(params["titles"].split("|"))
        titles = params["titles"].split("|")
        return {"query": {
            "redirects": [{"from": "File:Old.jpg", "to": "File:New.jpg"}],
            "pages": [page("File:New.jpg") if t == "File:Old.jpg" else page(t) for t in titles],
        }}

    resolver = make_resolver(fetch, batch_size=2)
    resolver.lookup(["File:Old.jpg", "File:B.jpg", "File:C.jpg", "File:B.jpg"])
    assert [len(c) for c in calls] == [2, 1]
    assert resolver.get("File:Old.jpg")["credit"]["file_page_url"].endswith("File:New.jpg")
    resolver.lookup(["File:B.jpg"])  # memoized: no new request
    assert len(calls) == 2


def test_resolver_failures_stay_unresolved_and_rate_limit_stops_lookups():
    resolver = make_resolver(lambda *a, **k: (_ for _ in ()).throw(FetchError("boom")))
    resolver.lookup(["File:A.jpg"])
    assert resolver.get("File:A.jpg") is None and not resolver.disabled
    limited = make_resolver(lambda *a, **k: (_ for _ in ()).throw(RateLimitedError("429")), batch_size=1)
    limited.lookup(["File:A.jpg", "File:B.jpg"])
    assert limited.disabled


def test_apply_image_policy_outcomes():
    pages = {
        "File:Marie Curie, 1920.jpg": page("File:Marie Curie, 1920.jpg"),
        "File:Herzberg,Gerhard 1952 London.jpg": page("File:Herzberg,Gerhard 1952 London.jpg", "GFDL"),
    }
    items = [item(THUMB), item(ORIGINAL), item(LOCAL_EN), item(None), item("https://example.com/a.jpg")]
    stats = apply_image_policy(items, make_resolver(commons_fetch(pages)))
    assert stats == {"kept": 1, "rejected:license:GFDL": 1, "not-commons": 2, "no-image": 1}
    assert items[0]["image_url"] == THUMB and items[0]["image_credit"]["license"] == "CC BY-SA 4.0"
    assert all(i["image_url"] is None and i["image_credit"] is None for i in items[1:])


def test_apply_image_policy_reuses_previous_credit_and_drops_unresolved():
    credit = {"author": "A", "license": "CC0", "license_url": None, "file_page_url": "https://commons.wikimedia.org/wiki/File:X"}
    calls = []
    resolver = make_resolver(commons_fetch({}, calls))
    items = [item(THUMB), item(ORIGINAL)]
    stats = apply_image_policy(items, resolver, [{"image_url": THUMB, "image_credit": credit}])
    assert items[0]["image_credit"] == credit  # known: not looked up again
    assert calls == [["File:Herzberg,Gerhard 1952 London.jpg"]]
    assert stats["kept"] == 1 and stats["unresolved"] == 1 and items[1]["image_url"] is None


def test_apply_image_policy_without_resolver_keeps_no_unknown_images():
    items = [item(THUMB)]
    assert apply_image_policy(items, None)["unresolved"] == 1 and items[0]["image_url"] is None


def test_registry_disables_dayinhistory_and_documents_licences():
    config = json.loads((ROOT / "sources" / "sources.json").read_text())
    enabled = {s["id"] for s in config["sources"] if s["enabled"]}
    assert enabled == {"wikimedia", "muffinlabs", "wikimedia_dyk"}
    for source in config["sources"]:
        assert source["license"] and source["terms_url"] and "attribution" in source
    assert config["legal"]["dataset_license"]["spdx"] == "CC-BY-SA-4.0"


def test_day_legal_block_only_credits_used_sources():
    config = json.loads((ROOT / "sources" / "sources.json").read_text())
    block = legal.day_legal_block(config, ["wikimedia"])
    assert [s["id"] for s in block["sources"]] == ["wikimedia"]
    assert "Wikipedia contributors" in block["attribution"] and "Muffin Labs" not in block["attribution"]
    both = legal.day_legal_block(config, ["wikimedia", "muffinlabs"])
    assert "Muffin Labs" in both["attribution"] and "Not endorsed by the Wikimedia Foundation" in both["attribution"]
    manifest = legal.manifest_legal_block(config)
    assert [s["id"] for s in manifest["sources"]] == ["wikimedia", "muffinlabs", "wikimedia_dyk"] and manifest["image_policy"]
    assert legal.day_article_url(2, 29) == "https://en.wikipedia.org/wiki/February_29"


@pytest.fixture
def annotate_pipeline(tmp_path):
    p = pipeline.build_pipeline(ROOT, data_dir=tmp_path, only_sources=["wikimedia"], resolve_images=False)
    p.image_resolver = make_resolver(commons_fetch({}))
    return p


def legacy_day(tmp_path):
    doc = {
        "date": "11-23", "display_date": "November 23", "last_updated": "2025-11-23T00:00:00Z",
        "sources_used": ["wikimedia"],
        "did_you_know": [
            {"id": f"dyk-11-23-0000000{i}", "fact": f"On November 23, 19{i}0: something quite surprising happened in history.",
             "tag": "History Trivia", "image_url": None, "source_url": None}
            for i in range(3)
        ],
        "items": [
            {"id": f"11-23-19{i}0-0000000{i}", "year": 1900 + i * 10, "year_display": str(1900 + i * 10), "category": "events",
             "title": f"Event {i}", "summary": f"Event {i}", "image_url": url, "source_url": None,
             "is_featured": True, "providers": ["wikimedia"]}
            for i, url in enumerate([LOCAL_EN, THUMB, None])
        ],
    }
    (tmp_path / "11-23.json").write_text(json.dumps(doc))
    return doc


def test_annotate_day_adds_legal_blocks_and_applies_image_policy(annotate_pipeline, tmp_path):
    legacy_day(tmp_path)
    annotate_pipeline.image_resolver = make_resolver(commons_fetch({"File:Marie Curie, 1920.jpg": page("File:Marie Curie, 1920.jpg")}))
    assert annotate_pipeline.annotate_day(11, 23) == "annotated"
    doc = json.loads((tmp_path / "11-23.json").read_text())
    assert doc["license"]["spdx"] == "CC-BY-SA-4.0" and doc["sources"][0]["id"] == "wikimedia"
    urls = [i["image_url"] for i in doc["items"]]
    assert urls == [None, THUMB, None]  # non-Commons image dropped, free Commons image kept with credit
    assert doc["items"][1]["image_credit"]["author"] == "Jane Doe"
    assert all(i["source_url"] == "https://en.wikipedia.org/wiki/November_23" for i in doc["items"])
    assert validate_data.validate_file(tmp_path / "11-23.json", annotate_pipeline.schema_validator) == []
    assert annotate_pipeline.annotate_day(11, 23) == "already-annotated"


def test_annotate_day_leaves_file_untouched_when_lookup_fails(annotate_pipeline, tmp_path):
    legacy_day(tmp_path)
    before = (tmp_path / "11-23.json").read_text()
    annotate_pipeline.image_resolver = make_resolver(lambda *a, **k: (_ for _ in ()).throw(FetchError("down")))
    assert annotate_pipeline.annotate_day(11, 23) == "annotate-unresolved-kept-existing"
    assert (tmp_path / "11-23.json").read_text() == before


def test_validator_flags_missing_legal_block_and_uncredited_images(annotate_pipeline, tmp_path):
    legacy_day(tmp_path)
    errors = validate_data.validate_file(tmp_path / "11-23.json", annotate_pipeline.schema_validator)
    assert any("legal block 'license'" in e for e in errors)
    assert any("image_url must be a Commons file" in e for e in errors)


def test_committed_fixture_documents_remain_valid_for_the_app_contract():
    # The Flutter app reads only these fields; the legal additions must stay optional in the schema.
    schema = json.loads((ROOT / "schema" / "higle" / "higle-day.schema.json").read_text())
    assert set(schema["required"]) == {"date", "display_date", "last_updated", "sources_used", "items"}
    assert "image_credit" not in schema["$defs"]["event"]["required"]
