"""Run every scrapable source in the registry and write ``data/raw/<slug>.json``.

Sources are declared in ``scrapers/sources.py``; each names a platform adapter
in ``scrapers/platforms/``. Adapters expose::

    scrape(*, source: str, manager: str, account: str, **kwargs) -> list[Unit]

Sources are scraped concurrently (different hosts, so the per-host throttle in
``base.polite_get`` is unaffected) because the daily GitHub Actions run would
otherwise spend most of its wall-clock waiting on network round-trips.

**Defensive write**: if a source returns zero units but its existing raw file
has units in it, the existing file is preserved. A portal that 503s or quietly
changes its markup would otherwise wipe good data from the live site. The
tradeoff is that a manager who genuinely rents out every unit keeps showing
stale listings until they list something new -- ``--allow-empty`` overrides
this, and the staleness is surfaced in the UI via ``scraped_at``.

Exit code is always 0 so the merge + commit steps in CI still run when one
source fails. Failures are summarized on stdout and written into
``data/raw/_status.json`` so the dashboard can show which sources are healthy.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import importlib
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = REPO_ROOT / "scrapers"
RAW_DIR = REPO_ROOT / "data" / "raw"

# Make scrapers/ and scrapers/platforms/ importable so adapters can do
# `from base import ...` without package plumbing.
sys.path.insert(0, str(SCRAPERS_DIR))
sys.path.insert(0, str(SCRAPERS_DIR / "platforms"))

from base import PACIFIC, ScrapeResult, write_result  # noqa: E402
from sources import SOURCES, Source, scrapable_sources  # noqa: E402

STATUS_PATH = RAW_DIR / "_status.json"


def _existing_unit_count(slug: str) -> int:
    path = RAW_DIR / f"{slug}.json"
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text()).get("units") or [])
    except (json.JSONDecodeError, OSError):
        return 0


def _source_url(src: Source) -> str:
    if src.platform == "appfolio":
        return f"https://{src.account}.appfolio.com/listings"
    if src.platform == "buildium":
        return f"https://{src.account}.managebuilding.com/Resident/public/rentals"
    return src.site


def scrape_one(src: Source, *, allow_empty: bool = False) -> dict:
    """Scrape a single source. Returns a status dict; never raises."""
    status = {
        "slug": src.slug,
        "manager": src.manager,
        "platform": src.platform,
        "ok": False,
        "units": 0,
        "error": None,
        "preserved": False,
    }
    try:
        adapter = importlib.import_module(src.platform)
        units = adapter.scrape(
            source=src.slug, manager=src.manager, account=src.account
        )

        if not units and not allow_empty:
            existing = _existing_unit_count(src.slug)
            if existing > 0:
                status.update(ok=True, units=existing, preserved=True)
                print(f"  [{src.slug}] 0 units -- preserved existing {existing}")
                return status

        result = ScrapeResult(
            source=src.slug,
            manager=src.manager,
            source_url=_source_url(src),
            units=units,
        )
        out = write_result(result)
        status.update(ok=True, units=len(result.units))
        print(f"  [{src.slug}] {len(result.units)} units -> {out.name}")
    except Exception as e:  # noqa: BLE001  (one bad source must not stop the run)
        status["error"] = f"{type(e).__name__}: {e}"
        print(f"  [{src.slug}] FAILED: {status['error']}")
        if "--traceback" in sys.argv:
            traceback.print_exc()
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="comma-separated slugs to scrape (default: all)")
    ap.add_argument("--workers", type=int, default=8, help="concurrent sources")
    ap.add_argument(
        "--allow-empty",
        action="store_true",
        help="overwrite a raw file even when the scrape returns zero units",
    )
    ap.add_argument("--traceback", action="store_true", help="print full tracebacks")
    args = ap.parse_args()

    targets = scrapable_sources()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        targets = [s for s in targets if s.slug in wanted]
        missing = wanted - {s.slug for s in targets}
        if missing:
            print(f"unknown or non-scrapable slug(s): {', '.join(sorted(missing))}",
                  file=sys.stderr)
        if not targets:
            return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"scraping {len(targets)} source(s) with {args.workers} workers\n")

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        statuses = list(
            ex.map(lambda s: scrape_one(s, allow_empty=args.allow_empty), targets)
        )

    ok = [s for s in statuses if s["ok"]]
    failed = [s for s in statuses if not s["ok"]]
    total_units = sum(s["units"] for s in ok)

    STATUS_PATH.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(PACIFIC).isoformat(timespec="seconds"),
                "sources_ok": len(ok),
                "sources_failed": len(failed),
                "total_units": total_units,
                "unscrapable": [
                    {"slug": s.slug, "manager": s.manager, "note": s.note}
                    for s in SOURCES
                    if not s.scrapable
                ],
                "sources": sorted(statuses, key=lambda s: s["slug"]),
            },
            indent=2,
        )
    )

    print(f"\n{len(ok)}/{len(targets)} sources ok, {total_units} units total")
    if failed:
        print(f"{len(failed)} failed: {', '.join(s['slug'] for s in failed)}")
    # Always 0 -- see module docstring.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
