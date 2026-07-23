# Bay Area Rentals

Pulls **available rental units** from Bay Area property managers once a day
and shows them on one filterable page. For every unit it collects the four
facts you actually shop on:

**city · bedrooms · move-in date · price**

Plus address, square footage, a link back to the listing, and which manager
posted it.

Currently **~830 units from 28 property managers across 39 cities**.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       28 property manager sites                     │
└───────┬─────────────────┬──────────────┬───────────────┬────────────┘
        │                 │              │               │
   ┌────▼─────┐     ┌─────▼─────┐  ┌─────▼──────┐  ┌─────▼─────────┐
   │ AppFolio │     │ Buildium  │  │  ShowMojo  │  │ 5 bespoke     │
   │ adapter  │     │ adapter   │  │  adapter   │  │ site adapters │
   │ 16 sites │     │  6 sites  │  │   1 site   │  │ (tripalink,   │
   └────┬─────┘     └─────┬─────┘  └─────┬──────┘  │  relisto, eri,│
        │                 │              │         │  mmg, boston) │
        │                 │              │         └─────┬─────────┘
        └─────────────────┴──────┬───────┴───────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  scrapers/sources.py     │
                    │  declarative registry —  │
                    │  one line per manager    │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  scrapers/base.py        │
                    │  polite HTTP · field     │
                    │  parsers · classifier    │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  scrapers/schema.py      │
                    │  Unit · validate · dedupe│
                    └────────────┬─────────────┘
                                 │
                      data/raw/<manager>.json
                                 │
                    ┌────────────▼─────────────┐
                    │  scripts/enrich_all.py   │
                    │  detail-page fields +    │
                    │  summary + cross-check   │
                    │  (scrapers/llm.py)       │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  scripts/reconcile.py    │
                    │  apply detail-page value │
                    │  where the index was wrong│
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼─────────────┐
                    │  scripts/merge.py        │
                    │  drop non-units · dedupe │
                    │  across sources · facets │
                    └────────────┬─────────────┘
                                 │
                        data/merged.json
                                 │
                    ┌────────────▼─────────────┐
                    │  web/  — filters, charts │
                    │  and the listings table  │
                    └──────────────────────────┘
```

**Two passes: index, then detail.** The adapters read each manager's *index*
page — the fast, cheap source of the headline facts (address, beds, rent,
date). Amenities and room type aren't there; they live on each listing's own
*detail* page, in prose. A second pass (`scripts/enrich_all.py`) reads those,
fills six fields — `room_type`, `pets`, `furnished`, `parking`, `laundry`, and
a human `summary` — and while it has the page open, reads the parser's headline
fields back to catch the index's mistakes. See
[Enrichment](#enrichment-amenities-room-type-and-a-cross-check).

**Why adapters instead of 28 scrapers.** Most property managers don't build
their own listing site — they rent one. 22 of the 28 sources resolve to just
three SaaS portals, all of which serve the same markup for every customer. So
the project is a declarative registry plus a few platform adapters, and adding
another AppFolio manager is a one-line change:

```python
Source("hayes_management", "Hayes Management", "https://hayesm.com/available/",
       "appfolio", "hayesmanagement"),
```

**No browser required.** Every source serves server-rendered HTML or JSON, so
the scrapers use only `urllib` and `re` from the standard library. The
enrichment pass talks to the Claude API the same way — raw `urllib`, no SDK —
so the zero-dependency rule holds even there. Nothing to install means fewer
ways for an unattended daily job to break.

## Quick start

```bash
git clone <this repo>
cd pm_data
pip install -r requirements.txt      # pytest only; scrapers need no deps

python3 scripts/run_all_scrapers.py  # scrape all sources  -> data/raw/*.json
python3 scripts/enrich_all.py        # amenities+summaries -> data/raw/*.json
python3 scripts/reconcile.py         # apply cross-check   -> data/raw/*.json
python3 scripts/merge.py             # combine + facet     -> data/merged.json
python3 scripts/serve.py             # -> http://127.0.0.1:8765/web/
```

The enrichment step needs `ANTHROPIC_API_KEY` set. Without it the pipeline
still runs — `enrich_all.py --no-llm` fills the structured fields and contacts
and skips the model — so the scrape, merge, and dashboard never depend on the
API being reachable.

Useful flags:

```bash
python3 scripts/run_all_scrapers.py --only hayes_management,raj_properties
python3 scripts/run_all_scrapers.py --workers 4 --traceback
python3 scripts/enrich_all.py --estimate     # price a run, spend nothing
python3 scripts/enrich_all.py --batch        # Batches API, 50% cheaper
python3 scripts/enrich_all.py --no-llm       # structured fields + contacts only
python3 -m pytest tests/ -q
```

## Daily refresh

`.github/workflows/refresh.yml` runs at **13:20 UTC daily** (~06:20 PT). It
runs the tests, scrapes, merges, commits the data if anything changed, and
deploys the page to GitHub Pages. It can also be triggered by hand from the
Actions tab.

To enable it on a fresh fork: **Settings → Pages → Source: GitHub Actions**.

## What gets collected

| Field | Notes |
|---|---|
| `city` | Normalized: `San francisco` and `SAN FRANCISCO` collapse to one facet |
| `bedrooms` | `0` = studio. Floats allowed — some sources publish `1.5` |
| `available_date` | ISO `YYYY-MM-DD` |
| `available_now` | Tracked separately — "Now" and a date are different claims |
| `rent` | USD/month. A range yields the low end |
| `unit_type` | `studio` / `1_bedroom` / `2_bedroom` / `3plus_bedroom`, derived from `bedrooms` |
| `room_type` | `entire_place` / `private_room` / `shared_room`, or `null` when undetermined |
| `pets`, `furnished`, `parking`, `laundry` | From the detail page — see [Enrichment](#enrichment-amenities-room-type-and-a-cross-check). `null` means the source was silent; `"none"` means it stated the amenity is absent — the two are different |
| `summary` | One or two plain sentences from the listing's detail page |
| `address`, `state`, `postal_code`, `bathrooms`, `square_feet`, `title`, `url` | Context |
| `kind` | `residential`, `room`, or an excluded kind (raw files only — dropped from `merged.json`, where `room_type` carries what a renter needs) |

Field coverage on the current run is **99% city, 96% bedrooms, 97% rent,
95% availability**. The gaps are real gaps in the sources, not parse failures
— MMG publishes no move-in date at all, and some managers say "call for
pricing". A unit missing one field is still listed, because a listing with an
unknown move-in date is still a listing worth seeing.

### Listings that aren't apartments

Portals push more than apartments through the same feed. `merge.py` drops
these from the dashboard (they stay in `data/raw/` for traceability):

- **`application`** — AppFolio "Rental Application" and "roommate switch only,
  NOT ON MARKET" placeholders, which are forms rather than vacancies
- **`parking`**, **`storage`** — parking stalls and storage lockers, several
  listed at $125–$250/mo
- **`commercial`** — offices, retail and medical suites

The classifier is careful in the direction that matters: a listing is only
tagged parking/storage/commercial if it *also* lacks a real bedroom count.
28 listings in the current dataset advertise "1 bedroom **with parking**",
and every one stays residential.

**`room`** is kept, not dropped, but tagged. Co-living and SRO operators rent a
single room inside a shared apartment — Tripalink's `floorPlanBedroomNum` says
`3.0` for an $810 room in a 3-bedroom flat. Letting that through as a "3 bed"
would wreck the bedroom filter, so rooms are labelled `Private room in 3 bd`
and the **Whole units only** checkbox hides them. Median rent is computed over
whole units only for the same reason.

## Enrichment: amenities, room type, and a cross-check

The index page gives the headline facts cheaply. Everything else a renter
wants — is it furnished, is there laundry, can I keep a cat, is this a whole
place or one room — is on the listing's *detail* page, written as prose.
`scripts/enrich_all.py` fetches those pages and fills the rest.

**Two tiers, no regex over prose.** Some sources hand you the answer already:
Tripalink's JSON carries a pet policy, AppFolio labels a listing
`"Shared Room in 1 bd"`. Those are read directly — free, deterministic, no
request. Everything else goes to Claude (`scrapers/llm.py`), which reads the
page and returns the fields plus the summary. An earlier version pattern-matched
the prose with regex; it produced every extraction bug and saved nothing (the
page is fetched for its summary regardless), so it was deleted. What it knew —
the phrasings that mislead, like "Double Occupancy: $900" being an upsell price
rather than a description — moved into the model's prompt.

**Cost is controlled by a cache, not by sampling.** Each answer is keyed by the
listing's URL in `data/enrich_cache.json`, so an unchanged listing is never
re-sent. The first backfill of ~570 pages costs about **$0.83** on Haiku 4.5
via the Batches API; a daily run after that re-queries only the handful of
listings that actually changed, for a fraction of a cent. It also keeps a
committed raw file from rewriting its summary every day for no reason.

**The cross-check is the interesting part.** While the model has the detail
page open, it also reads back the fields the *index parser* produced — `kind`,
`bedrooms`, `rent`, `available_date` — independently. Disagreements are
recorded in `data/conflicts.json`, and `scripts/reconcile.py` applies the
detail-page value wherever the page is the better source (it usually is: the
index truncates). `rent` and `square_feet` are left for manual review — the
parser's "low end of a range" is deliberate, not a bug.

This is how the classifier's blind spots surface at scale. The classifier keys
off keywords; a listing titled `Dwight 3A - BED IN TRIPLE` contains none it
knows, so it counted 80 shared rooms as whole apartments — their per-bed rents
polluting the bedroom medians — until the cross-check flagged all 80 at once.
Two readers of the same facts, from different sources, catch what neither would
alone.

**Contacts** are collected per manager (`data/contacts.json`, joined on
`manager` in `merged.json`), not stored on every unit — one office phone serves
all of a manager's listings. Email extraction is scoped past `<script>` tags
and HTML comments, because a naïve sweep pulls addresses out of analytics
payloads.

## Sources

### Working (28)

| Platform | Managers |
|---|---|
| **AppFolio** (16) | AP Management · Authentic Properties · Bay Cities · Beacon Properties · Boardwalk · California Pacific · Cedar Properties · Gordon PM · Hayes Management · Kasa Properties · Korman & Ng · Myerhoff & Associates · North Berkeley Properties · Panoramic Berkeley · Perkins Realty · SG Real Estate · Structure Properties · 2B Living |
| **Buildium** (6) | Academic Housing Rentals · CollabHome · Domingo Properties · MPM Oakland · Raj Properties · Square One Management |
| **ShowMojo** (1) | Premium Properties |
| **Bespoke** (5) | Tripalink (Next.js JSON) · ReListo (WP REST) · ERI (custom PHP) · MMG (Realtyna WPL) · Boston PM (WP theme) |

A few managers front a portal under a different name than their public site —
SG Real Estate's inventory lives on `sgrealestate.appfolio.com`, Panoramic's on
`panoramicmgt.appfolio.com`, Premium Properties' RentManager portal is
login-only so the public ShowMojo feed is used instead. `sources.py` records
the mapping.

### Investigated, not scrapable (4)

Kept in `sources.py` with a `note` so the coverage table stays honest.

| Manager | Why |
|---|---|
| **Hudson McDonald** | Publishes no unit-level inventory anywhere. Its Buildium public site is disabled (redirects to tenant login) and the WordPress site has no listing post type — only per-building "from $1,800*" marketing pages with no addresses or dates. |
| **The Berkeley Group** | Entrata ProspectPortal behind a Cloudflare managed challenge. ~59 student-housing floorplans are visible when the challenge clears, but that needs a headless browser on a residential IP — not dependable from CI. |
| **Vindium Real Estate** | Yardi RentCafe behind two Cloudflare layers; `rentcafe.com` hard-blocks datacenter IPs. No HTTP-only path exists. |
| **Oxford Apartments** | One hand-authored Wix page with a single vacancy. Bedrooms and date are published but **rent is withheld sitewide** — zero dollar amounts in 776 KB of HTML. |

## Adding a source

If it's an AppFolio or Buildium account, add one line to `SOURCES` in
`scrapers/sources.py` with the portal subdomain. To find it, look for
`*.appfolio.com` or `*.managebuilding.com` in the manager's page source, then
confirm the account serves listings:

```bash
curl -s https://<account>.appfolio.com/listings | grep -c js-listing-item
curl -s https://<account>.managebuilding.com/Resident/public/rentals | grep -c featured-listing__title
```

Otherwise add `scrapers/platforms/<name>.py` exposing:

```python
def scrape(*, source: str, manager: str, account: str | None, **kwargs) -> list[Unit]:
    ...
```

Build `Unit`s with the parsers from `base.py` (`parse_rent`, `parse_bedrooms`,
`parse_available`, `parse_location`, `classify_listing`) rather than hand-rolled
regexes — that's what keeps one schema across 28 differently-worded sources.
Add cases to `tests/test_parsers.py` for any new wording.

## Layout

```
scrapers/
  schema.py              Unit dataclass, controlled vocabularies, validation,
                         dedupe, write_result()
  base.py                polite_get, field parsers, listing classifier
  sources.py             the registry — one Source per manager
  enrich.py              structured-field readers, contact extraction,
                         cross-check comparison, page + answer caches
  llm.py                 stdlib Claude client (Messages + Batches API)
  platforms/
    appfolio.py          16 managers
    buildium.py           6 managers
    showmojo.py           1 manager
    tripalink.py  relisto.py  eri.py  mmg.py  boston_pm.py
scripts/
  run_all_scrapers.py    concurrent scrape, defensive write, _status.json
  enrich_all.py          detail-page pass: amenities, summary, cross-check
  reconcile.py           apply detail-page values over the index parser
  merge.py               filter non-units, dedupe, facets -> merged.json
  serve.py               local static server + POST /api/refresh
web/
  index.html  app.js  style.css
data/
  raw/<manager>.json     one per source, committed
  raw/_status.json       per-source health for the last run
  enrich_cache.json      model answers keyed by listing URL (cost control)
  conflicts.json         parser/detail-page disagreements, for review
  contacts.json          per-manager phone + agent emails
  merged.json            what the page reads
tests/
  test_parsers.py        137 cases over the parsers, classifier, and enrichment
```

## Notes on reliability

- **Defensive write** — a source returning zero units does *not* overwrite a
  non-empty raw file. A portal that 503s or silently changes its markup would
  otherwise wipe good data from the live site. Override with `--allow-empty`.
- **Always exits 0** — one failing source must not stop the merge and commit,
  or a single broken portal would freeze the whole dashboard.
- **Per-host throttle** — at most one request per second to any single host;
  different hosts proceed in parallel.
- **Staleness is shown, not hidden** — `merge.py` flags any source whose data
  is more than 48h old and the page marks it.
- **Sanity bounds** — rent outside $100–$100,000 or bedrooms outside 0–20 is
  rejected as a parse bug rather than published.

## Caveats

Rents and availability change constantly and listings go stale between daily
runs — always confirm with the manager. Scraping only covers what each manager
publishes publicly; several list inventory on Zillow or Apartments.com that
never appears on their own site. `data/raw/` is committed so the daily diff
doubles as a record of how listings change over time.
