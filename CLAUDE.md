# CLAUDE.md — project context

## What this is

A daily scraper + static dashboard for available Bay Area rental units. ~830
units from 28 property managers, each reduced to a common `Unit` record whose
headline fields are **city, bedrooms, move-in date, rent**.

Read `README.md` first — it has the architecture diagram, the source table,
and the list of managers we deliberately can't scrape.

## The one design decision that explains the layout

22 of 28 managers run on three SaaS portals (AppFolio, Buildium, ShowMojo),
which serve identical markup per customer. So this is **a declarative registry
(`scrapers/sources.py`) plus a few platform adapters**, not 28 scraper files.
Adding an AppFolio manager is one line. Only genuinely bespoke sites get their
own module in `scrapers/platforms/`.

Everything uses the **standard library only** (`urllib`, `re`). No Playwright,
no requests, no BeautifulSoup — every source serves server-rendered HTML or
JSON. `pytest` is the only dependency and only for tests. Keep it that way
unless a source truly cannot be reached otherwise; a dependency-free daily job
has far fewer ways to break.

## Adapter contract

```python
# scrapers/platforms/<name>.py
PLATFORM = "<name>"

def scrape(*, source: str, manager: str, account: str | None, **kwargs) -> list[Unit]:
    ...
```

`run_all_scrapers.py` imports the module named by `Source.platform` and calls
`scrape()`. Adapters return `list[Unit]`; the runner wraps them in a
`ScrapeResult` and writes the file. Adapters must not write files themselves.

Split parsing from fetching (`parse_listings(html, ...)` next to `scrape()`)
so a saved fixture can be tested without network.

## Where correctness lives

`scrapers/base.py`. Every source words the same four facts differently, and
these functions are what collapse that into one schema:

| Function | Handles |
|---|---|
| `parse_rent` | `$3,295` · `1200.00` · `$2,000 - $3,000` (returns low end) |
| `parse_bedrooms` | `2 bd / 1 ba` · `2 Bed \| 1 Bath` · `Studio` → `0.0` · bare `2` |
| `parse_available` | `8/2/26` · `available September 1` · `NOW` → `(iso, is_now)` |
| `parse_location` | `…, Berkeley, CA 94709` · Buildium's `Fresno,CA\|93726` |
| `normalize_city` | Collapses casing drift; rejects streets/ZIPs in the city slot |
| `classify_listing` | residential / room / parking / storage / commercial / application |

**Always use these rather than a local regex.** `tests/test_parsers.py` has 100
cases, most of them real strings from live feeds. Add to it when you add a
source with new wording.

### Traps that already bit

- **Studio is `0.0`, which is falsy.** Never `a or b` to combine bedroom
  values — use the `_coalesce` helper in `buildium.py` or an explicit
  `is not None`. A studio silently became "unknown" this way once.
- **Year-less dates.** "available September 1" has no year. `_infer_year`
  prefers the current year and only rolls forward once the date is >60 days
  past, so "available July 1" on July 22 stays 2026 (already open) rather than
  becoming 2027.
- **`available_now` is separate from `available_date` on purpose.** "Now" stays
  true tomorrow; a date silently rots. Don't collapse them.
- **Tripalink's `rentWhole` lies.** It reads `"1"` on per-room listings too.
  The real signal is a `rooms` array of length 1 against a multi-bedroom floor
  plan — see `platforms/tripalink.py`.
- **Dates in JS.** `new Date("2026-08-01")` parses as UTC and renders as Jul 31
  in Pacific. `web/app.js` has `parseISODate` for this; use it.

## The classifier, and why it's conservative

Portals list parking stalls, storage lockers, commercial suites and "Rental
Application" placeholders alongside apartments. `classify_listing` tags them
and `merge.py` drops non-livable kinds from `merged.json` (raw files keep
everything).

The asymmetry is deliberate: **parking/storage/commercial patterns only fire
when the listing has no real bedroom count.** 28 current listings say
"1 bedroom with parking" and all must stay residential. Application patterns
fire regardless of bedrooms, because those placeholders do carry bedroom
counts.

`room` is kept but tagged — it's real housing, but an $810 "3 bedroom" that is
one room in a shared flat would destroy the bedroom filter. The UI labels it
`Room in 3 bd`, the **Whole units only** filter hides it, and median rent
excludes it.

## Data flow

```
sources.py registry → platform adapter → list[Unit]
  → schema.write_result()   validate, dedupe, sort → data/raw/<slug>.json
  → scripts/merge.py        drop non-units, cross-source dedupe, facets
  → data/merged.json        → web/app.js
```

`run_all_scrapers.py` also writes `data/raw/_status.json` (per-source health +
the unscrapable list), which the page reads for its "Managers we can't pull
data from" section.

## Conventions

- **Timezone is Pacific.** `schema.PACIFIC`; JS mirrors it.
- **`available_date` is ISO `YYYY-MM-DD`**, never a locale string.
- **Raw files are committed.** The daily diff is a record of market change.
- **Units are sorted** by (city, bedrooms, rent, address) in `write_result` so
  daily diffs stay readable — an unstable order makes every run look rewritten.
- **Defensive write**: zero units never overwrites a non-empty raw file.
- **`run_all_scrapers.py` always exits 0** so one dead portal can't block the
  merge + commit. Failures land in `_status.json`.
- **Sanity bounds** (`schema.MIN_RENT` / `MAX_RENT` / `MAX_BEDROOMS`) exist to
  catch parser bugs, not to filter unusual listings — keep them wide.

## Frontend

`web/app.js` is dependency-free ES5-style JS; charts are DOM divs sized by
percentage, themed entirely through CSS custom properties. Colors come from a
validated data-viz palette (blue `#2a78d6`/`#3987e5` primary, orange
`#eb6834`/`#d95926` secondary) that passes contrast and colorblind checks in
both light and dark mode — if you change them, re-validate rather than
eyeballing. Text always uses ink tokens, never a series color.

Charts and the median-rent tile compute over **whole units only**; mixing
per-room co-living prices into a "3 bed" median understates it badly.

## Gotcha when running locally

The page fetches `../data/merged.json`, so it must be served over HTTP, not
opened as a `file://` URL. Use `python3 scripts/serve.py`.
