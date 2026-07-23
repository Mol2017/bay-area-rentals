#!/usr/bin/env python3
"""Second pass over data/raw/: amenities, room type, contact, summaries.

Runs after ``run_all_scrapers.py`` and before ``merge.py``. The scrape reads
index pages; this reads each listing's own detail page and fills the fields
that only live there.

    python3 scripts/enrich_all.py --estimate      # price it, spend nothing
    python3 scripts/enrich_all.py --batch         # backfill, 50% cheaper
    python3 scripts/enrich_all.py                 # daily delta
    python3 scripts/enrich_all.py --no-llm        # contacts only, no key needed

Structured fields the adapters already captured (Tripalink's pet policy,
AppFolio's room labels) are kept as-is and never re-derived -- they cost
nothing and are more reliable than a reading of prose. Everything else goes
to the model in one call per listing, which also writes the summary.

Cost control is the cache, not sampling: a listing whose url, title and rent
are unchanged since the last run is not re-sent. After the first backfill
that leaves the 30-60 listings that actually moved. Nothing is truncated
silently -- if a run declines to enrich something, it says so.

Exits 0 even when enrichment fails. A dead API key or a rate limit must
degrade the dashboard's detail, never block the merge and commit.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = REPO_ROOT / "scrapers"
sys.path.insert(0, str(SCRAPERS_DIR))
sys.path.insert(0, str(SCRAPERS_DIR / "platforms"))

import enrich  # noqa: E402
import llm  # noqa: E402
from schema import ENRICHED_ASSIGNABLE, RAW_DIR  # noqa: E402

# Rough per-listing input size, refined by --estimate against real pages.
DEFAULT_AVG_TOKENS = 5_000


def load_raw() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(RAW_DIR.glob("*.json"))
        if not path.stem.startswith("_")
    }


def enrichable(doc: dict) -> list[dict]:
    """Units worth spending a request on.

    Non-livable kinds are excluded: merge.py drops parking stalls and
    application forms from the dashboard, so summarising them buys nothing.
    """
    return [
        u for u in doc.get("units", [])
        if u.get("url") and u.get("kind") in ("residential", "room")
    ]


def structured_of(unit: dict) -> enrich.Enrichment:
    """What the adapters already established, without a request."""
    return enrich.Enrichment(
        room_type=unit.get("room_type"),
        pets=unit.get("pets"),
        furnished=unit.get("furnished"),
        parking=unit.get("parking"),
        laundry=unit.get("laundry"),
        summary=unit.get("summary"),
        method="structured" if unit.get("pets") or unit.get("room_type") else "none",
    )




# How far the two readers may differ before it counts as a disagreement.
# Rent gets a tolerance because index and detail pages legitimately quote
# different ends of a range; bedrooms and kind get none, because those are
# the values that silently poison medians and filters when wrong.
RENT_TOLERANCE = 0.05


def find_conflicts(unit: dict, observed: dict) -> list[dict]:
    """Compare the index parser against the detail page, without resolving.

    Only meaningful when the page is about this one unit. Tripalink's 271
    units share 11 building pages, and a building page cannot confirm any
    single unit's rent -- comparing against it would manufacture 260
    conflicts that are really just the wrong question, which is why the model
    is asked to set `describes_one_unit` first.

    Nothing here overwrites the parser. Every bug found while building this
    was a confident wrong value with nothing to contradict it; the point of a
    second reader is to make those visible, and auto-picking a winner would
    put us back where we started.
    """
    if not observed or not observed.get("describes_one_unit"):
        return []

    out: list[dict] = []

    def note(field, parsed, seen):
        out.append({"field": field, "parsed": parsed, "observed": seen})

    beds = observed.get("bedrooms")
    if beds is not None and unit.get("bedrooms") is not None and beds != unit["bedrooms"]:
        note("bedrooms", unit["bedrooms"], beds)

    rent = observed.get("rent")
    if rent and unit.get("rent"):
        if abs(rent - unit["rent"]) / max(rent, unit["rent"]) > RENT_TOLERANCE:
            note("rent", unit["rent"], rent)

    kind = observed.get("listing_kind")
    if kind and kind != "unknown" and kind != unit.get("kind"):
        note("kind", unit.get("kind"), kind)

    city = observed.get("city")
    if city and unit.get("city") and city.strip().lower() != unit["city"].strip().lower():
        note("city", unit["city"], city)

    return out


def apply_to_unit(unit: dict, result: dict) -> None:
    """Write model output onto a raw unit, without overwriting known values."""
    for field in ENRICHED_ASSIGNABLE:
        if unit.get(field) is None and result.get(field) is not None:
            unit[field] = result[field]
    if result.get("summary") and not unit.get("summary"):
        unit["summary"] = result["summary"]
    conflicts = find_conflicts(unit, result.get("observed") or {})
    if conflicts:
        unit["conflicts"] = conflicts


def collect(args) -> tuple[list[dict], dict, dict]:
    """Fetch each distinct detail page once and build one payload per page.

    Grouped by URL, not by unit. The model's whole input is the page, so
    units sharing a page share an answer -- Tripalink's 271 units live on 11
    property pages, and asking per-unit would buy 260 duplicate responses at
    full price.

    Returns (payloads, meta-by-url, context).
    """
    raw = load_raw()
    cache = enrich.load_cache()
    pages = enrich.load_page_cache()
    reused = 0
    payloads: list[dict] = []
    meta: dict[str, dict] = {}
    contacts: dict[str, dict] = {}
    stats = Counter()

    # url -> the units that point at it, grouped across all sources.
    groups: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for slug, doc in raw.items():
        units = enrichable(doc)
        stats["units"] += len(units)
        for unit in units:
            groups[unit["url"]].append((slug, unit))
    stats["pages"] = len(groups)

    # Sorted so page ids are stable between runs: a rerun that re-queues the
    # same pages assigns them the same ids, which keeps batch results
    # comparable and diffs readable.
    for url, members in sorted(groups.items()):
        key = enrich.cache_key(url)
        if key in cache:
            stats["cached"] += len(members)
            continue
        if args.limit and stats["queued"] >= args.limit:
            stats["over_limit"] += len(members)
            continue

        hit = pages.get(url)
        if hit:
            text, reused = hit["text"], reused + 1
        else:
            text = enrich.fetch_detail_text(url)
            if text:
                pages[url] = {"text": text, "at": __import__("time").time()}
                # Flush periodically so a kill mid-fetch keeps what we have.
                if len(pages) % 25 == 0:
                    enrich.save_page_cache(pages)
        if not text:
            stats["fetch_failed"] += len(members)
            continue

        slug, first = members[0]
        # Contact details are manager-level; one page per source suffices.
        if slug not in contacts:
            contacts[slug] = {
                "manager": raw[slug].get("manager"),
                "source_url": url,
                **enrich.extract_contact(text),
            }

        # The batch API caps custom_id at 64 chars and listing URLs routinely
        # exceed that, so pages get a short positional id and the url is
        # carried in `meta` instead.
        pid = "p%05d" % len(payloads)

        known = structured_of(first)
        payloads.append({
            "id": pid,
            "address": first.get("address"),
            "title": first.get("title"),
            "text": text,
            "known": {f: getattr(known, f) for f in enrich.ENRICHED_FIELDS
                      if getattr(known, f)},
        })
        meta[pid] = {"url": url, "members": members, "key": key,
                     "structured": known}
        stats["queued"] += 1
        stats["queued_units"] += len(members)

    enrich.save_page_cache(pages)
    stats["fetch_reused"] = reused
    return payloads, meta, {"contacts": contacts, "stats": stats, "raw": raw, "cache": cache}


def estimate(payloads: list[dict], ctx: dict, args) -> None:
    """Price the run from measured page sizes. Spends nothing."""
    stats = ctx["stats"]
    if not payloads:
        print("nothing queued -- everything is cached or already known")
        return

    chars = [len(p["text"]) for p in payloads]
    # ~4 chars/token for English prose; the system prompt adds ~700.
    tokens = [c / 4 + 700 for c in chars]
    total_in = sum(tokens)
    total_out = len(payloads) * 220          # schema'd JSON + evidence quotes

    print(f"\n  queued          {len(payloads)} pages "
          f"covering {stats['queued_units']} units")
    print(f"  page size       min {min(chars):,}  median {sorted(chars)[len(chars)//2]:,}  "
          f"max {max(chars):,} chars")
    print(f"  input tokens    ~{total_in/1e6:.2f}M   output ~{total_out/1e3:.0f}k")
    print(f"\n  {'model':<18}{'immediate':>12}{'batch (-50%)':>16}")
    print("  " + "-" * 46)
    for name, (rin, rout) in (
        ("Sonnet 5", (2.0, 10.0)),
        ("Opus 4.8", (5.0, 25.0)),
        ("Haiku 4.5", (1.0, 5.0)),
    ):
        cost = total_in / 1e6 * rin + total_out / 1e6 * rout
        print(f"  {name:<18}{'$' + format(cost, '.2f'):>12}{'$' + format(cost/2, '.2f'):>16}")

    print(f"\n  pages fetched by this estimate: {len(payloads)} "
          f"({stats['fetch_failed']} failed)")
    print(f"  units already cached: {stats['cached']}")
    if stats["over_limit"]:
        print(f"  NOT queued due to --limit {args.limit}: "
              f"{stats['over_limit']} units")


def run(payloads: list[dict], meta: dict, ctx: dict, args) -> int:
    model = {"opus": llm.MODEL_HARD, "sonnet": llm.MODEL_MID,
             "haiku": llm.MODEL_BULK}[args.model]
    cache, raw = ctx["cache"], ctx["raw"]

    if args.batch or args.submit_only:
        print(f"submitting {len(payloads)} pages to the batch API on {model} ...")
        batch_id = llm.submit_batch(payloads, model=model)
        print(f"  batch id: {batch_id}")
        if args.submit_only:
            print(f"  collect with: python3 scripts/enrich_all.py --collect {batch_id}")
            return 0
        llm.poll_batch(batch_id)
        results = llm.batch_results(batch_id)
    else:
        failures: list[str] = []
        results = llm.enrich_many(
            payloads, model=model,
            on_error=lambda item, e: failures.append(f"{item['id']}: {e}"),
        )
        for line in failures[:5]:
            print(f"  failed: {line}")
        if len(failures) > 5:
            print(f"  ... and {len(failures) - 5} more failures")

    return apply_results(results, meta, ctx, args)


def write_contacts(contacts: dict) -> None:
    """Persist manager-level leasing contacts for merge.py to join on."""
    path = REPO_ROOT / "data" / "contacts.json"
    path.write_text(json.dumps(contacts, indent=2, sort_keys=True))


def apply_results(results: dict, meta: dict, ctx: dict, args) -> int:
    """Merge model answers onto the raw files and persist caches."""
    cache, raw = ctx["cache"], ctx["raw"]
    applied = Counter()
    conflicts = Counter()
    conflict_rows: list[dict] = []
    for pid, result in results.items():
        if pid not in meta:
            continue        # page no longer queued (cache hit since submit)
        entry = meta[pid]
        # Merge against each unit's own structured values, not the group's:
        # units on a shared page can differ in room_type even where the page
        # text is identical.
        answer = enrich.Enrichment(
            **{f: result.get(f) for f in enrich.ENRICHED_FIELDS},
            method="llm", evidence=result.get("evidence"),
        )
        cache[entry["key"]] = {
            **{f: getattr(answer, f) for f in enrich.ENRICHED_FIELDS},
            "method": answer.method,
            "evidence": answer.evidence,
            "observed": result.get("observed") or {},
        }
        observed = result.get("observed")
        for slug, unit in entry["members"]:
            merged = structured_of(unit).merge(answer)
            apply_to_unit(unit, {f: getattr(merged, f)
                                 for f in enrich.ENRICHED_FIELDS})
            # Only meaningful when this page maps to exactly one unit --
            # compare_observed also refuses on multi-unit pages, but checking
            # here keeps the intent visible at the call site.
            if len(entry["members"]) == 1:
                # Recorded in the standalone report only -- the per-unit
                # `conflicts` field was removed from the schema, so a
                # disagreement lives in data/conflicts.json where it can be
                # reviewed without bloating every record.
                found = enrich.compare_observed(unit, observed)
                for c in found:
                    conflicts[c["type"]] += 1
                    if c["type"] == "conflict":
                        conflict_rows.append({"address": unit.get("address"),
                                              "manager": unit.get("manager"),
                                              "url": unit.get("url"), **c})
            applied[slug] += 1

    # Replay the cache onto every unit, including ones not queued this run.
    for slug, doc in raw.items():
        for unit in enrichable(doc):
            hit = cache.get(enrich.cache_key(unit.get("url")))
            if hit:
                apply_to_unit(unit, {**hit})

    for slug, doc in raw.items():
        (RAW_DIR / f"{slug}.json").write_text(json.dumps(doc, indent=2))
    enrich.save_cache(cache)

    # Written whether or not anything disagreed: an empty report is a
    # meaningful result, and a missing file is indistinguishable from a run
    # that never checked.
    report = REPO_ROOT / "data" / "conflicts.json"
    report.write_text(json.dumps(
        {"checked": sum(1 for m in meta.values() if len(m["members"]) == 1),
         "conflicts": conflicts.get("conflict", 0),
         "parser_missing": conflicts.get("parser_missing", 0),
         "rows": sorted(conflict_rows, key=lambda r: (r["field"], r["address"] or ""))},
        indent=2))

    write_contacts(ctx["contacts"])

    print(f"\nenriched {sum(applied.values())} units from {len(results)}/{len(meta)} "
          f"pages across {len(applied)} sources")
    print(f"contacts written for {len(ctx['contacts'])} managers")
    print(f"cross-check: {conflicts.get('conflict', 0)} conflicts, "
          f"{conflicts.get('parser_missing', 0)} fields the page had and the "
          f"parser missed -> {report.name}")
    for row in conflict_rows[:8]:
        print(f"    {row['field']:15} parser={row['parser']!r} page={row['page']!r}  "
              f"{(row['address'] or '')[:40]}")
    if len(conflict_rows) > 8:
        print(f"    ... and {len(conflict_rows) - 8} more in {report.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--estimate", action="store_true",
                    help="fetch pages and price the run without calling the model")
    ap.add_argument("--batch", action="store_true",
                    help="use the Batches API (50%% cheaper, completes within the hour)")
    ap.add_argument("--model", choices=("haiku", "sonnet", "opus"), default="haiku",
                    help="haiku for high-volume passes; opus only for the "
                         "small set of genuinely ambiguous listings")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap listings queued this run (0 = no cap)")
    ap.add_argument("--results-file", metavar="PATH", default=None,
                    help="apply batch results already downloaded to a .jsonl file")
    ap.add_argument("--use-batch", metavar="ID", default=None,
                    help="adopt an already-submitted batch instead of creating "
                         "a new one (recovers a run whose poller died)")
    ap.add_argument("--no-llm", action="store_true",
                    help="structured fields and contacts only; no key required")
    # Submit and collect are split so a long batch survives across
    # invocations: a cron that dies mid-poll can resume with --collect
    # instead of paying for the whole run again.
    ap.add_argument("--submit-only", action="store_true",
                    help="submit a batch, print its id, and exit without polling")
    ap.add_argument("--collect", metavar="BATCH_ID",
                    help="apply the results of an already-finished batch")
    ap.add_argument("--warm", action="store_true",
                    help="populate the page cache only; no model call")
    args = ap.parse_args()

    if args.collect:
        status = llm.batch_status(args.collect)
        if status.get("processing_status") != "ended":
            print(f"batch {args.collect}: {status.get('processing_status')} "
                  f"-- {status.get('request_counts')}")
            return 0
        payloads, meta, ctx = collect(args)
        return apply_results(llm.batch_results(args.collect), meta, ctx, args)

    payloads, meta, ctx = collect(args)
    stats = ctx["stats"]
    print(f"{stats['units']} enrichable units on {stats['pages']} distinct pages | "
          f"queued {stats['queued']} pages ({stats['queued_units']} units) | "
          f"cached {stats['cached']} units | reused {stats['fetch_reused']} fetches | "
          f"fetch failed {stats['fetch_failed']}")

    if args.warm:
        print(f"page cache warmed ({stats['fetch_reused']} reused this run)")
        return 0
    if args.estimate:
        estimate(payloads, ctx, args)
        return 0
    if args.no_llm:
        # Contacts come from page text, not the model, so this path still
        # produces them -- that is the point of --no-llm. But only overwrite
        # the existing file when this run actually fetched pages; a --no-llm
        # run against a warm cache has no page text and would otherwise
        # replace real contacts with an empty file.
        if ctx["contacts"]:
            write_contacts(ctx["contacts"])
            print(f"--no-llm: contacts written for {len(ctx['contacts'])} managers")
        else:
            print("--no-llm: no pages fetched this run; kept existing contacts")
        return 0
    if not payloads and not (args.use_batch or args.results_file):
        print("nothing to do")
        return 0

    try:
        return run(payloads, meta, ctx, args)
    except llm.LLMUnavailable as e:
        # Never block the merge. The dashboard loses detail, not listings.
        print(f"enrichment unavailable: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
