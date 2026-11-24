# Attribution and third-party notices

> Best-effort compliance information, **not legal advice**. Review the linked terms yourself.

The dataset in `data/` is licensed **CC BY-SA 4.0** (see [LICENSE-DATA.md](LICENSE-DATA.md)); the code is MIT
(see [LICENSE](LICENSE)). This file lists every upstream source.

## Sources in use

### Wikimedia On This Day API (Wikipedia content)

* Source id: `wikimedia` - <https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/all/{MM}/{DD}>
* Content: English Wikipedia text, © Wikipedia contributors (authors per article history).
* Licence: [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) (and GFDL; we use CC BY-SA 4.0) -
  [Wikimedia Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use),
  [Reusing Wikipedia content](https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content).
* API terms: <https://api.wikimedia.org/wiki/Terms_and_conditions>; User-Agent policy:
  <https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy>; rate limits:
  <https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits>.
* Required attribution: credit Wikipedia (link to the article through each item's `source_url`), link the licence,
  indicate changes.
* Wikimedia Commons image metadata (author, licence) is read through the Commons API
  (<https://commons.wikimedia.org/w/api.php>).

### Wikimedia featured feed - "Did you know" (Wikipedia content)

* Source id: `wikimedia_dyk` - <https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{YYYY}/{MM}/{DD}>
* Content: the `dyk` hooks of English Wikipedia's main page, © Wikipedia contributors.
* Licence, terms, User-Agent and rate-limit rules: as for the Wikimedia On This Day API above (CC BY-SA 4.0).
* Required attribution: credit Wikipedia, link the article via the fact's `source_url`, indicate changes.
* The page-summary API (<https://en.wikipedia.org/api/rest_v1/page/summary/{title}>) is used to find the thumbnail of
  the hook's article; the image rules in "Images" below apply.

### Muffin Labs History API

* Source id: `muffinlabs` - <https://history.muffinlabs.com/date/{M}/{D}>
* Content: Wikipedia "Events on this day" data re-published by Muffin Labs.
* Licence: [CC BY-SA 3.0 US](https://creativecommons.org/licenses/by-sa/3.0/us/) per
  <https://history.muffinlabs.com/#license> ("attributed and any alterations must be shared under a compatible
  license"). CC BY-SA 4.0 is a compatible licence for adaptations of BY-SA 3.0 works.
* Required attribution: credit Muffin Labs and Wikipedia; indicate changes.

## Sources not in use

### dayinhistory.dev - **disabled** (`enabled: false` in `sources/sources.json`)

* <https://dayinhistory.dev/> - its pages state "All rights reserved", no redistribution licence is published, the
  free tier is for "personal projects and learning" (10 requests/hour) and production use requires a paid plan.
* No data in `data/` was derived from it (every day file lists only `wikimedia` and `muffinlabs` in `sources_used`).
* The adapter is kept for reference. **Do not enable it for the public dataset unless you obtain written permission
  or a suitable plan from the provider** (contact listed on <https://dayinhistory.dev/pricing>).

## Images

* `image_url` hotlinks to `upload.wikimedia.org` / `thumb.wikimedia.org` files under `/wikipedia/commons/`.
  Nothing is copied into this repository.
* Local English-Wikipedia uploads (`/wikipedia/en/`, often non-free "fair use") and any other host are dropped.
* A Commons file is kept only if its licence is Public domain, CC0, CC BY or CC BY-SA. Files with other,
  non-free or unknown licences are dropped.
* `image_credit` holds `author` (may be null), `license`, `license_url` (may be null) and `file_page_url` (the
  Commons description page, the canonical place for the full credit). CC BY / CC BY-SA images require showing
  author, licence and a link wherever the image is displayed; the dataset's CC BY-SA 4.0 does not apply to them.
* Some Commons files carry non-copyright restrictions (trademark, personality rights, insignia). They are
  informational; the file page lists them.

## Modifications made to source data

Selected and filtered; HTML removed; years parsed (negative = BCE); headline titles shortened (about 100
characters, ellipsis; summaries keep the source wording); events merged and de-duplicated across sources (fuzzy matching);
ranked, "featured" flagged, capped (max 100 per day with category quotas) and sorted; `id`s generated
deterministically; `did_you_know` facts are either Wikipedia "Did you know" hooks (cleaned to plain text, leading
"..." normalised) or short adaptations of events/holidays (reworded into `On <date>, <year>: <summary>.`, trimmed to
240 characters, tagged by keyword rules); `source_url` falls back to the Wikipedia date article when absent.

## Trademarks and endorsement

Wikipedia, Wikimedia and Wikimedia Commons are trademarks of the Wikimedia Foundation. This project is not
affiliated with or endorsed by the Wikimedia Foundation, Muffin Labs or Day in History.
