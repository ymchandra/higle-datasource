import json
from datetime import date

import pytest

import legal
import pipeline
import validate_data
from conftest import ROOT, load_fixture
from http_client import FetchError, RateLimitedError


@pytest.fixture
def make_pipeline(tmp_path, monkeypatch):
    """Pipeline writing into tmp_path with all upstream HTTP replaced by fixtures."""

    def factory(responses):
        def fake_get_json(url, *, user_agent, params=None):
            for needle, response in responses.items():
                if needle in url:
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise FetchError(f"no fixture for {url}")

        monkeypatch.setattr(pipeline, "get_json", fake_get_json)
        # dayinhistory is disabled in the registry (no licence); tests opt in explicitly.
        p = pipeline.build_pipeline(
            ROOT, data_dir=tmp_path, only_sources=["wikimedia", "dayinhistory", "muffinlabs"], resolve_images=False
        )
        p.sleep = lambda _: None
        monkeypatch.setattr("time.sleep", lambda _: None)
        return p

    return factory


def all_ok():
    return {
        "wikimedia.org": load_fixture("wikimedia_11-23.json"),
        "muffinlabs": load_fixture("muffinlabs_11-23.json"),
        "dayinhistory": {"next": None, "results": load_fixture("dayinhistory_11-23.json")["events"]},
    }


def read(tmp_path, key="11-23"):
    return json.loads((tmp_path / f"{key}.json").read_text())


def test_writes_valid_merged_file(make_pipeline, tmp_path):
    p = make_pipeline(all_ok())
    assert p.process_day(11, 23) == "written"
    doc = read(tmp_path)
    assert doc["date"] == "11-23" and doc["display_date"] == "November 23"
    assert doc["sources_used"] == ["wikimedia", "dayinhistory", "muffinlabs"]
    doctor_who = next(i for i in doc["items"] if i["year"] == 1963)
    assert set(doctor_who["providers"]) == {"wikimedia", "dayinhistory", "muffinlabs"}
    assert sum(i["is_featured"] for i in doc["items"]) >= 3
    assert validate_data.validate_file(tmp_path / "11-23.json", p.schema_validator) == []


def test_unchanged_run_does_not_rewrite(make_pipeline, tmp_path):
    p = make_pipeline(all_ok())
    p.process_day(11, 23)
    before = (tmp_path / "11-23.json").read_text()
    assert make_pipeline(all_ok()).process_day(11, 23) == "unchanged"
    assert (tmp_path / "11-23.json").read_text() == before


def test_all_sources_failing_preserves_existing_file(make_pipeline, tmp_path):
    make_pipeline(all_ok()).process_day(11, 23)
    before = (tmp_path / "11-23.json").read_text()
    failing = {"wikimedia.org": FetchError("boom"), "muffinlabs": FetchError("boom"),
               "dayinhistory": RateLimitedError("429")}
    p = make_pipeline(failing)
    assert p.process_day(11, 23) == "failed-kept-existing"
    assert (tmp_path / "11-23.json").read_text() == before
    assert make_pipeline(failing).process_day(11, 24) == "failed-no-data"
    assert not (tmp_path / "11-24.json").exists()


def test_html_error_page_from_one_source_is_skipped(make_pipeline, tmp_path):
    responses = all_ok()
    responses["wikimedia.org"] = "<html><body>Service Unavailable</body></html>"
    p = make_pipeline(responses)
    assert p.process_day(11, 23) == "written"
    assert read(tmp_path)["sources_used"] == ["dayinhistory", "muffinlabs"]


def test_degraded_run_keeps_richer_existing_file(make_pipeline, tmp_path):
    make_pipeline(all_ok()).process_day(11, 23)
    before = (tmp_path / "11-23.json").read_text()
    responses = all_ok()
    responses["wikimedia.org"] = FetchError("down")
    assert make_pipeline(responses).process_day(11, 23) == "degraded-kept-existing"
    assert (tmp_path / "11-23.json").read_text() == before


def test_rate_limited_source_is_disabled_for_the_rest_of_the_run(make_pipeline):
    responses = all_ok()
    responses["dayinhistory"] = RateLimitedError("429")
    p = make_pipeline(responses)
    p.process_day(11, 23)
    dih = next(s for s in p.sources if s.id == "dayinhistory")
    assert dih.disabled_reason == "rate limited"
    assert p.fetch_all(11, 24).keys() <= {"wikimedia", "muffinlabs"}


def test_full_mode_skips_populated_days_unless_forced(make_pipeline, tmp_path):
    p = make_pipeline(all_ok())
    p.skip_existing = True
    p.process_day(11, 23)
    assert p.process_day(11, 23) == "skipped-existing"
    assert p.process_day(11, 23, force=True) == "unchanged"


def test_ids_stay_stable_when_title_is_rephrased(make_pipeline, tmp_path):
    make_pipeline(all_ok()).process_day(11, 23)
    old_ids = {i["id"] for i in read(tmp_path)["items"]}
    responses = all_ok()
    responses["muffinlabs"] = {"data": {"Events": [], "Births": [], "Deaths": []}}  # no items -> sources shrink
    responses["wikimedia.org"] = {"events": [
        {"text": "The 1963 premiere of the science-fiction series Doctor Who was broadcast on BBC television.",
         "year": 1963, "pages": []}]}
    responses["dayinhistory"] = {"next": None, "results": []}
    p = make_pipeline(responses)
    p.process_day(11, 23, force=True)
    new = read(tmp_path)
    assert new["items"][0]["id"] in old_ids


def test_manifest(make_pipeline, tmp_path):
    p = make_pipeline(all_ok())
    p.process_day(11, 23)
    block = legal.manifest_legal_block(p.config)
    manifest = pipeline.update_manifest(tmp_path, legal_block=block)
    assert manifest["total_days"] == 1 and manifest["schema_version"] == "1.0.0"
    assert manifest["days"]["11-23"]["event_count"] == len(read(tmp_path)["items"])
    assert manifest["total_events"] == manifest["days"]["11-23"]["event_count"]
    written = (tmp_path / "manifest.json").read_text()
    pipeline.update_manifest(tmp_path, legal_block=block)  # idempotent: generated_at does not move without changes
    assert (tmp_path / "manifest.json").read_text() == written
    assert validate_data.validate_manifest(tmp_path, [tmp_path / "11-23.json"]) == []


def test_rolling_and_full_day_lists():
    assert pipeline.rolling_days(today=date(2025, 11, 23)) == [(11, 22), (11, 23), (11, 24)]
    assert pipeline.rolling_days(today=date(2025, 1, 1)) == [(12, 31), (1, 1), (1, 2)]
    assert pipeline.rolling_days(today=date(2025, 3, 1)) == [(2, 28), (3, 1), (3, 2)]
    assert pipeline.rolling_days((2, 29), today=date(2025, 6, 1)) == [(2, 28), (2, 29), (3, 1)]
    assert pipeline.rolling_days((3, 15), today=date(2025, 6, 1)) == [(3, 14), (3, 15), (3, 16)]
    days = pipeline.full_days()
    assert len(days) == 366 and (2, 29) in days
    assert pipeline.full_days(2, 2)[-1] == (2, 29)
    assert pipeline.parse_month_day("02-29") == (2, 29)


def test_validate_data_rejects_bad_files(tmp_path, make_pipeline):
    p = make_pipeline(all_ok())
    p.process_day(11, 23)
    good = read(tmp_path)

    bad = json.loads(json.dumps(good))
    bad["items"][0]["category"] = "wars"
    bad["items"][1]["image_url"] = "http://insecure/x.jpg"
    bad["items"][2]["title"] = "x" * 141
    (tmp_path / "11-23.json").write_text(json.dumps(bad))
    assert len(validate_data.validate_file(tmp_path / "11-23.json", p.schema_validator)) >= 3

    mismatch = {**good, "date": "11-24"}
    (tmp_path / "11-23.json").write_text(json.dumps(mismatch))
    assert any("does not match" in e for e in validate_data.validate_file(tmp_path / "11-23.json", p.schema_validator))

    (tmp_path / "11-23.json").write_text("<html>")
    assert validate_data.validate_file(tmp_path / "11-23.json", p.schema_validator)
    assert validate_data.main(["--data-dir", str(tmp_path)]) == 1
