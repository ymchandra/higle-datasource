# higle-datasource

Automated, zero-server data pipeline that pulls historical "On This Day" events from several open APIs,
validates them against JSON Schemas, merges and deduplicates them, and commits one static JSON file per
calendar day (`data/01-01.json` … `data/12-31.json`, including `02-29.json`) for the **Higle** Flutter app.
The app reads the files through the jsDelivr CDN and also bundles an offline copy.

## Using the data

### CDN URL pattern

```
https://cdn.jsdelivr.net/gh/ymchandra/higle-datasource@main/data/{MM-DD}.json
https://cdn.jsdelivr.net/gh/ymchandra/higle-datasource@main/data/manifest.json
```

Example: <https://cdn.jsdelivr.net/gh/ymchandra/higle-datasource@main/data/11-23.json>

jsDelivr caches branch references (`@main`) for up to ~12 hours; purge via
`https://purge.jsdelivr.net/gh/ymchandra/higle-datasource@main/data/11-23.json` if you need it sooner.
Use a release tag (`@v1.0.0`) for immutable URLs.

### Day file format (`schema/higle/higle-day.schema.json`)

```jsonc
{
  "date": "11-23",
  "display_date": "November 23",
  "last_updated": "2025-11-23T00:05:12Z",
  "sources_used": ["wikimedia", "muffinlabs"],
  "did_you_know": [                    // 3-5 fun facts per day (optional in the schema, always emitted)
    {
      "id": "dyk-11-23-a1b2c3d4",      // dyk-{MM}-{DD}-{sha1(normalized fact)[:8]}, stable while the text is
      "fact": "On November 23, 1992: The first smartphone, the IBM Simon, is introduced at COMDEX.", // plain text, 40-240 chars
      "tag": "Inventions",             // topic badge: Science, Space, Nature, Sports, Culture, Oddities, Mystery & Crime, Inventions, History Trivia
      "image_url": "https://…",        // Commons image or null (apps must hide it when image_credit is missing)
      "source_url": "https://…",       // originating Wikipedia article or null
      "image_credit": { /* same shape as items[].image_credit; only present with image_url */ },
      "providers": ["wikimedia", "muffinlabs"]  // optional: which sources the fact came from
    }
  ],
  "items": [
    {
      "id": "11-23-1963-2835d641",     // {MM}-{DD}-{year}-{sha1(normalized title)[:8]}, stable across updates
      "year": 1963,                    // negative = BCE (e.g. -44); 0 for year-less holidays
      "year_display": "1963",          // "44 BCE" for negative years, "" for holidays
      "category": "events",            // events | births | deaths | holidays
      "title": "…",                    // max 140 chars
      "summary": "…",                  // plain text, HTML stripped
      "image_url": "https://…",        // https or null
      "source_url": "https://…",       // https/http or null
      "is_featured": true,             // 3–5 notable items per day
      "providers": ["wikimedia", "muffinlabs"]  // sources that confirmed the event
    }
  ]
}
```

Items are sorted by year (newest first by default; `output.sort_order` in `sources/sources.json`), holidays last,
and capped at 100 per day (`output.max_items_per_day`, with per-category quotas).

#### `did_you_know` (fun facts)

Two tiers fill it (`scripts/fun_fact_extractor.py`, wired through `deduplicator.build_did_you_know`):

1. **Wikipedia "Did you know…" hooks** from the Wikimedia featured feed
   (`https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{YYYY}/{MM}/{DD}`, source `wikimedia_dyk`), formatted
   as `...that X?`. As verified in November 2026 the feed only returns a `dyk` array for **today and upcoming
   dates** (it is absent for past dates), entries carry only `text`/`html` (no thumbnails; the article comes from
   the bolded link and a thumbnail is looked up through the Wikipedia page-summary API and licence-checked like
   every other image), and the hooks are Wikipedia main-page items rather than "this day in history" facts. So it
   is requested only within +-2 days of the run date (`output.did_you_know.fetch_window_days`) and hooks are kept
   in the file afterwards (provider `wikimedia_dyk`).
2. **Deterministic heuristic fallback** whenever the hooks give fewer than 3: non-featured events/holidays of the
   day are scored on curiosity keywords (*first, invented, accidental, record, banned, discovered, unusual…*), grim
   topics are down-ranked, at most two per tag are taken first, and the text is formatted as
   `On November 23, 1992: <summary>.` with an inferred `tag`. Same input gives the same output, so daily diffs stay
   minimal. Facts never duplicate each other or a featured item (same fuzzy match as the deduplicator).

Facts of the second kind reuse the `image_url`, `image_credit` and `source_url` of the item they come from. A fact
image is always a Commons file with `image_credit`, otherwise `null`. The 3-5 count, id format, text length and
image rules are enforced by the schema and `scripts/validate_data.py`.

`data/manifest.json` holds `schema_version`, `version`, `generated_at`, `total_days` (366 when complete),
`total_events` and per-day `last_updated` / `event_count` / `did_you_know_count`.

### Offline bundle

`python scripts/export_bundle.py` zips `data/*.json` into `higle-offline-bundle.zip`; drop its contents into the
Flutter app's `assets/data/`. Pushing a tag like `v1.0.0` runs `.github/workflows/release-bundle.yml`, which attaches
the bundle to a GitHub release.

## Sources (`sources/sources.json`)

Sources can be toggled, re-prioritised or extended without touching pipeline code. Priority decides whose title wins
when events are merged.

| id | endpoint | notes |
| --- | --- | --- |
| `wikimedia` | `https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{MM}/{DD}` | events, births, deaths, holidays, featured ("selected") entries, images. Needs the descriptive User-Agent. |
| `dayinhistory` | `https://api.dayinhistory.dev/v1/events/{month_name}/{D}/` | Paginated; month is the lowercase English name. **The anonymous tier is limited to 10 requests/hour per IP**, so only the events endpoint is fetched by default (`extra_endpoints` can add `births`/`deaths`) and the source is switched off for the rest of a run after the first HTTP 429. Full backfills therefore get little or no data from this source; the daily workflow (3 requests per run) usually fits. |
| `wikimedia_dyk` | `https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{YYYY}/{MM}/{DD}` | `kind: "did_you_know"`: only its `dyk` array is used (see "did_you_know" above). Never needed for a day to be written; when unavailable the heuristic fallback is used. |
| `muffinlabs` | `https://history.muffinlabs.com/date/{M}/{D}` | events, births, deaths. Its "holidays" rows are mis-parsed upstream and are ignored. |

URL placeholders: `{MM}`/`{DD}` zero-padded, `{M}`/`{D}` not padded, `{month_name}` lowercase month name.

The User-Agent in `sources.json` is `HigleDatasourceBot/1.0 (https://github.com/ymchandra/higle-datasource; contact@example.com)`.
**Replace `contact@example.com` with a real contact address** (Wikimedia asks for one).

## How the pipeline works (`scripts/`)

1. `pipeline.py` fetches every enabled source (in parallel, honouring `rate_limit_delay_sec`), retrying HTTP 429/5xx
   and network errors with exponential backoff (3 retries: 1s, 2s, 4s, honouring `Retry-After`).
2. Each raw payload is validated against its `schema/<source>/response.schema.json`. HTML error pages, changed
   structures etc. are logged as warnings and that source is skipped; the remaining sources carry on.
3. `adapters/*` transform the payload into Higle events.
4. `deduplicator.py` groups by exact year + category, fuzzy-matches normalised titles/summaries
   (`rapidfuzz.token_set_ratio >= 75`), merges duplicates (longest summary, keeps image / source URL /
   `is_featured`, unions `providers`), makes sure at least 3 events are featured (promoting events with images),
   caps and sorts.
5. The result is validated against `schema/higle/higle-day.schema.json` and written to `data/MM-DD.json`; then
   `data/manifest.json` is refreshed.

Safety rules: an existing file is never replaced when all sources fail, when the result would be empty, or when
fewer sources answered than produced the existing file (a degraded run). Files are only rewritten when their content
(ignoring `last_updated`) actually changes, so unchanged days produce no commits. If a refreshed event matches an
event in the previous file, the previous `id` is reused so bookmarks stay valid.

### Running locally

```bash
pip install -r requirements-dev.txt
python scripts/pipeline.py --mode rolling                 # yesterday, today, tomorrow (UTC)
python scripts/pipeline.py --mode rolling --date 11-23    # around a given date
python scripts/pipeline.py --mode full --start-month 1 --end-month 12 [--force]
python scripts/pipeline.py --mode dyk [--date 11-23] [--force]   # (re)populate did_you_know in existing files, offline unless within +-2 days of today
python scripts/validate_data.py [--require-all]
python scripts/check_config.py                            # sources.json + schema syntax
python -m pytest
```

## GitHub Actions (`.github/workflows/`)

| workflow | trigger | purpose |
| --- | --- | --- |
| `daily-update.yml` | cron `0 0,12 * * *`, manual (optional `date` MM-DD) | Refresh yesterday/today/tomorrow (UTC) incl. `did_you_know` (`--mode dyk`), validate, commit `chore(data): update daily history events [skip ci]` and push. |
| `full-backfill.yml` | manual (`start_month`, `end_month`, `force_overwrite`) | Populate all 366 days and their `did_you_know` facts, validate, commit once. Run it on setup or after adding a source. |
| `validate-pr.yml` | PRs/pushes touching `schema/**`, `sources/**`, `scripts/**`, … | JSON/schema syntax checks, unit tests with mock fixtures, data validation. |
| `release-bundle.yml` | tag `v*` | Builds and attaches `higle-offline-bundle.zip` to a GitHub release. |

The data-writing workflows need **Settings → Actions → General → Workflow permissions** to allow pushing to `main`
(they request `contents: write`). If `main` is protected, allow the Actions bot to bypass the rule.

## Legal / Attribution

> **Best-effort compliance information, not legal advice.** If something here matters to you legally, check the
> linked terms or consult a lawyer.

| What | Licence | Where |
| --- | --- | --- |
| Code (`scripts/`, `tests/`, workflows, schemas) | MIT | [LICENSE](LICENSE) |
| Dataset (`data/*.json`) | **CC BY-SA 4.0** | [LICENSE-DATA.md](LICENSE-DATA.md) |
| Every upstream source, terms, required credit, modifications | - | [ATTRIBUTION.md](ATTRIBUTION.md) |
| Images (hotlinked from Wikimedia Commons) | per file (see `image_credit`) | each item's `image_credit.file_page_url` |

**Licensing decision.** Wikipedia text is CC BY-SA 4.0 (+GFDL); Muffin Labs states CC BY-SA 3.0, and CC BY-SA 4.0 is
a compatible licence for adaptations of BY-SA 3.0, so the whole derived dataset is offered under CC BY-SA 4.0.
`dayinhistory.dev` publishes no redistribution licence ("All rights reserved"; free tier for personal use), so that
source is **disabled** (`enabled: false`) and no data in `data/` comes from it.

**Images.** No image is stored here. `image_url` is kept only for Wikimedia Commons files (`/wikipedia/commons/`)
whose licence is Public domain, CC0, CC BY or CC BY-SA; local English-Wikipedia uploads (often non-free), other hosts
and files with unknown or other licences are dropped (`image_url: null`). Author, licence and Commons page are in
`image_credit`. Some files carry non-copyright restrictions (trademark, personality rights); see the file page.
`python scripts/pipeline.py --mode annotate` re-applies the image policy and legal blocks to existing files without
refetching the upstream APIs.

**Day files and manifest.** Each file also carries optional top-level `license`, `attribution`, `sources[]`,
`modifications` and `notice_url`, and items may have `image_credit`. The Flutter app's contract (`date`,
`display_date`, `last_updated`, `items[]`) is unchanged; the extra fields are optional.

**No endorsement / trademarks.** Wikipedia, Wikimedia and Wikimedia Commons are trademarks of the Wikimedia
Foundation. This project is not affiliated with or endorsed by the Wikimedia Foundation, Muffin Labs or Day in History.

**Wikimedia User-Agent.** Wikimedia requires an identifying User-Agent with contact details
([policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)); the pipeline sends the
one from `sources/sources.json`. It includes the repository URL, but **`contact@example.com` is a placeholder the
owner must replace** with a monitored address.

**Takedown / contact.** Rights holders or Wikimedia/Commons contributors who want content removed can contact
**`TAKEDOWN-CONTACT-PLACEHOLDER` (owner: replace with a monitored email before making the repo public)**. Include the
affected file/item `id` and the reason; matching items or images will be removed promptly, and upstream
corrections should be made at the source (Wikipedia/Commons).

**This repository does not accept external contributions** - see [CONTRIBUTING.md](CONTRIBUTING.md) and
`.github/CODEOWNERS`.

### What apps consuming the data must display

In the Higle app's About / Credits screen (and wherever the data is redistributed):

> Historical "On This Day" data from Wikipedia and Muffin Labs, licensed under CC BY-SA 4.0
> (https://creativecommons.org/licenses/by-sa/4.0/). Adapted and compiled by the Higle datasource project; changes
> were made. Not endorsed by the Wikimedia Foundation. Images are from Wikimedia Commons and keep their own licences.

* "Did you know" facts (`did_you_know[]`) are Wikipedia-derived text under the same licence: show the same credit,
  link `source_url` ("Read on Wikipedia"), and apply the image rules above to their `image_url` / `image_credit`.
* Make the credit text link to the licence and to Wikipedia; offer a "Read full source" link from `source_url`.
* For every shown image, display `image_credit` (author if present, licence name linked to `license_url`, and a link
  to `file_page_url`) near the image or in an image-credits screen; do not show images without `image_credit`.
* Hotlinking from `upload.wikimedia.org` / `thumb.wikimedia.org`: send an identifying User-Agent and cache locally;
  do not scrape at high rates.
* If you bundle an offline copy of `data/*.json`, ship the `LICENSE-DATA.md` / `ATTRIBUTION.md` text with it.
* Apps may keep their own code under any licence; only adaptations of the *dataset* must stay CC BY-SA 4.0.
