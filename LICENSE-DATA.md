# Dataset licence (`data/*.json`)

> Best-effort compliance information, **not legal advice**.

The files in `data/` (`01-01.json` … `12-31.json` and `manifest.json`) are an **adapted** collection of text
derived from Wikipedia and from the Muffin Labs "History" API (itself derived from Wikipedia). They are released
under the

**[Creative Commons Attribution-ShareAlike 4.0 International licence (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/)**
(legal code: <https://creativecommons.org/licenses/by-sa/4.0/legalcode>, SPDX `CC-BY-SA-4.0`).

## Why CC BY-SA 4.0

* Wikipedia text is dual-licensed CC BY-SA 4.0 and GFDL
  ([Wikimedia Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use)); reusers may choose CC BY-SA 4.0.
* Muffin Labs states its data is "licensed using CC BY-SA 3.0 … the same license used for Wikipedia data"
  ([history.muffinlabs.com](https://history.muffinlabs.com/#license)). Creative Commons lists BY-SA 4.0 as a
  licence compatible for adaptations of BY-SA 3.0 material (one-way: 3.0 → 4.0), so an adaptation combining both
  can be offered under BY-SA 4.0. One consistent licence for the whole dataset is the safest option.
* ShareAlike means anyone who adapts this dataset must also licence their adaptation under CC BY-SA 4.0 (or a
  compatible licence). Apps that merely *display* the data are not required to relicense their own code; they must
  credit the sources and keep the notices (see below).

## Required attribution

When you use or redistribute the data, give appropriate credit, link to the licence and indicate changes:

> Historical "On This Day" data from [Wikipedia](https://en.wikipedia.org/) (via the Wikimedia On This Day API) and
> [Muffin Labs](https://history.muffinlabs.com/), licensed under
> [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Compiled and adapted by the Higle datasource
> project (<https://github.com/ymchandra/higle-datasource>); changes were made. Not endorsed by the Wikimedia
> Foundation.

The same text (plus per-source credits) is embedded in every day file under `attribution`, `license`, `sources` and
`modifications`, and in `data/manifest.json`.

## Changes made (adaptation notice)

Entries were selected, normalised (HTML removed, years parsed, headline titles shortened to about 100 characters
with an ellipsis), merged and de-duplicated across sources, ranked, capped per day and category, and sorted.
Summaries keep the source wording. Image links are unmodified Wikimedia URLs.

## Images are **not** covered by this licence

`image_url` values are hotlinks to files on Wikimedia Commons; no image is stored in this repository. Each image
keeps its own licence and author. Only Commons files under Public domain, CC0, CC BY or CC BY-SA are kept; the
author, licence and Commons file page are recorded per item in `image_credit`. See [ATTRIBUTION.md](ATTRIBUTION.md).

## Source list

See [ATTRIBUTION.md](ATTRIBUTION.md) for every source, its licence, terms and the modifications made.
