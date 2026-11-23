import deduplicator as dd
from adapters.base_adapter import make_id, make_item


def item(year=1963, category="events", title="T", summary="S", provider="wikimedia", image=None,
         featured=False, source_url=None):
    return make_item(year=year, category=category, title=title, summary=summary, provider=provider,
                     image_url=image, is_featured=featured, source_url=source_url)


def test_merges_same_event_across_sources_and_enriches():
    wiki = item(title="First episode of Doctor Who broadcast on BBC", summary="Doctor Who debuts.",
                image="https://img/x.jpg", featured=True, source_url="https://en.wikipedia.org/wiki/Doctor_Who")
    muffin = item(title="The first episode of Doctor Who is broadcast by the BBC", provider="muffinlabs",
                  summary="The first episode of Doctor Who is broadcast by the BBC, starring William Hartnell.")
    merged = dd.deduplicate([wiki, muffin])
    assert len(merged) == 1
    result = merged[0]
    assert result["summary"] == muffin["summary"]  # more detailed summary wins
    assert result["title"] == wiki["title"]  # higher priority source keeps the title
    assert result["image_url"] == "https://img/x.jpg" and result["is_featured"] is True
    assert result["providers"] == ["wikimedia", "muffinlabs"]
    assert result["source_url"].endswith("Doctor_Who")


def test_image_and_featured_survive_when_lower_priority_source_has_them():
    plain = item(title="Battle of Hastings fought in England", summary="a")
    rich = item(title="Battle of Hastings fought in England", provider="muffinlabs", summary="a",
                image="https://img/h.jpg", featured=True)
    (result,) = dd.deduplicate([plain, rich])
    assert result["image_url"] == "https://img/h.jpg" and result["is_featured"]


def test_only_same_year_and_category_are_compared():
    a = item(year=1900, title="Famous scientist wins prize")
    assert len(dd.deduplicate([a, item(year=1901, title="Famous scientist wins prize")])) == 2
    assert len(dd.deduplicate([a, item(year=1900, category="births", title="Famous scientist wins prize")])) == 2


def test_distinct_events_in_same_year_stay_separate():
    items = [
        item(title="Earthquake destroys city of Lisbon", summary="Earthquake in Portugal kills thousands"),
        item(title="Treaty of Paris signed by delegates", summary="Treaty ends the war between nations"),
    ]
    assert len(dd.deduplicate(items)) == 2


def test_single_token_titles_need_exact_match():
    assert not dd.is_duplicate(item(title="War", summary=""), item(title="War of 1812 begins", summary=""))
    assert dd.is_duplicate(item(title="Madonna", summary=""), item(title="Madonna", summary=""))


def test_normalize_text_strips_punctuation_and_stop_words():
    assert dd.normalize_text("The Fall of Rome, in 476!") == "fall rome 476"


def test_ensure_featured_promotes_events_with_images_and_caps():
    items = [item(title=f"Event {n} happened", image="https://i/%d" % n if n % 2 else None) for n in range(6)]
    items.append(item(category="births", title="Some Person", image="https://i/p"))
    dd.ensure_featured(items, min_featured=3)
    featured = [i for i in items if i["is_featured"]]
    assert len(featured) == 3 and all(i["image_url"] and i["category"] == "events" for i in featured)

    many = [item(title=f"Selected {n} item", featured=True) for n in range(9)]
    dd.ensure_featured(many, min_featured=3, max_featured=5)
    assert sum(i["is_featured"] for i in many) == 5


def test_select_items_caps_with_quotas_and_keeps_featured():
    items = (
        [item(year=1000 + n, title=f"Event number {n}") for n in range(30)]
        + [item(year=1900 + n, category="births", title=f"Person {n}") for n in range(30)]
        + [item(year=1950 + n, category="deaths", title=f"Dead {n}") for n in range(30)]
    )
    items[29]["is_featured"] = True
    chosen = dd.select_items(items, max_items=20, quotas={"events": 10, "births": 6, "deaths": 4})
    assert len(chosen) == 20
    assert items[29] in chosen
    counts = {c: sum(i["category"] == c for i in chosen) for c in ("events", "births", "deaths")}
    assert counts == {"events": 10, "births": 6, "deaths": 4}
    assert dd.select_items(items[:5], max_items=20) == items[:5]


def test_sort_order_and_holidays_last():
    items = [item(year=-44, title="a"), item(year=1900, title="b"), item(year=0, category="holidays", title="h"),
             item(year=1500, title="c")]
    assert [i["year"] for i in dd.sort_items(items, "desc")] == [1900, 1500, -44, 0]
    assert [i["year"] for i in dd.sort_items(items, "asc")] == [-44, 1500, 1900, 0]


def test_ids_are_deterministic_and_unique():
    assert make_id(11, 23, 1963, "Doctor Who!") == make_id(11, 23, 1963, "doctor   who")
    assert make_id(11, 23, 1963, "x").startswith("11-23-1963-")
    assert make_id(3, 1, -44, "x").startswith("03-01--44-")
    items = [item(title="Same"), item(title="Same")]
    dd.assign_ids(items, 11, 23)
    assert items[0]["id"] != items[1]["id"] and items[1]["id"].endswith("-2")


def test_stabilize_ids_reuses_previous_id_for_rephrased_event():
    old = item(title="First episode of Doctor Who broadcast on BBC television")
    old["id"] = "11-23-1963-aaaaaaaa"
    new = item(title="The first episode of Doctor Who is broadcast on BBC television")
    new["id"] = "11-23-1963-bbbbbbbb"
    other = item(year=1990, title="Something else entirely here")
    other["id"] = "11-23-1990-cccccccc"
    dd.stabilize_ids([new, other], [old])
    assert new["id"] == "11-23-1963-aaaaaaaa" and other["id"] == "11-23-1990-cccccccc"


def test_process_pipeline_end_to_end():
    items = [item(year=1900 + n, title=f"Event {n} occurs", image="https://i/%d" % n) for n in range(10)]
    result = dd.process(items, 11, 23, max_items=5, quotas={"events": 5})
    assert len(result) == 5 and all(i["id"].startswith("11-23-") for i in result)
    assert sum(i["is_featured"] for i in result) >= 3
    assert [i["year"] for i in result] == sorted((i["year"] for i in result), reverse=True)
