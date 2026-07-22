"""Merge ``data/raw/*.json`` into a single ``data/merged.json`` for the frontend.

Reads every per-source raw file produced by ``run_all_scrapers.py``, flattens
their unit lists, drops cross-source duplicates, and writes one combined file
with pre-computed facets (cities, bedroom counts, rent range) so the page can
build its filter controls without scanning the whole dataset first.

Output schema (consumed by ``web/app.js``)::

    {
      "generated_at": "2026-07-22T09:15:00-07:00",
      "total_units": 412,
      "sources": [ {slug, manager, site, unit_count, scraped_at, stale}, ... ],
      "facets": {
        "cities":   [{"name": "Berkeley", "count": 88}, ...],
        "bedrooms": [{"value": 0, "label": "Studio", "count": 31}, ...],
        "rent":     {"min": 950, "max": 8500}
      },
      "units": [ ... ]
    }

Cross-source dedupe is deliberately conservative. Two managers legitimately
list the same building (a leasing agent and an owner), and collapsing those
would under-count real inventory -- so a duplicate must match on normalized
address *and* bedrooms *and* rent before we drop it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
OUT = REPO_ROOT / "data" / "merged.json"

sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from base import LIVABLE_KINDS  # noqa: E402
from schema import PACIFIC, _normalize_address  # noqa: E402
from sources import by_slug  # noqa: E402

# A source whose data is older than this is flagged in the UI rather than
# silently presented as current.
STALE_AFTER_HOURS = 48


def _bedroom_label(v) -> str:
    if v is None:
        return "Unknown"
    if v == 0:
        return "Studio"
    if float(v).is_integer():
        return f"{int(v)} bed"
    return f"{v} bed"


def main() -> int:
    if not RAW_DIR.exists():
        print(f"no raw dir at {RAW_DIR}", file=sys.stderr)
        return 1

    now = datetime.now(PACIFIC)
    all_units: list[dict] = []
    sources_meta: list[dict] = []
    seen: set[tuple] = set()
    excluded_kinds: dict[str, int] = {}

    for path in sorted(RAW_DIR.glob("*.json")):
        if path.name.startswith("_"):  # _status.json and friends
            continue
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"skip {path.name}: {e}", file=sys.stderr)
            continue

        slug = doc.get("source") or path.stem
        src = by_slug(slug)
        scraped_at = doc.get("scraped_at")

        stale = False
        if scraped_at:
            try:
                age = now - datetime.fromisoformat(scraped_at)
                stale = age > timedelta(hours=STALE_AFTER_HOURS)
            except ValueError:
                pass

        kept = 0
        for u in doc.get("units", []):
            # Parking stalls, storage lockers, commercial suites and
            # "Rental Application" pseudo-listings stay in the raw files for
            # traceability but never reach the dashboard.
            if u.get("kind", "residential") not in LIVABLE_KINDS:
                excluded_kinds[u.get("kind")] = excluded_kinds.get(u.get("kind"), 0) + 1
                continue
            key = (
                _normalize_address(u.get("address", "")),
                u.get("bedrooms"),
                u.get("rent"),
            )
            if key in seen:
                continue
            seen.add(key)
            all_units.append(u)
            kept += 1

        sources_meta.append(
            {
                "slug": slug,
                "manager": doc.get("manager") or (src.manager if src else slug),
                "site": src.site if src else None,
                "platform": src.platform if src else None,
                "unit_count": kept,
                "scraped_at": scraped_at,
                "stale": stale,
            }
        )

    # Sort: soonest-available first, then cheapest. Units with no date sort
    # last -- an unknown move-in date is the least actionable listing.
    all_units.sort(
        key=lambda u: (
            not u.get("available_now"),
            u.get("available_date") or "9999-99-99",
            u.get("rent") if u.get("rent") is not None else 9e9,
        )
    )

    city_counts: dict[str, int] = {}
    bed_counts: dict[float | None, int] = {}
    kind_counts: dict[str, int] = {}
    rents: list[float] = []
    for u in all_units:
        kind_counts[u.get("kind", "residential")] = (
            kind_counts.get(u.get("kind", "residential"), 0) + 1
        )
        if u.get("city"):
            city_counts[u["city"]] = city_counts.get(u["city"], 0) + 1
        bed_counts[u.get("bedrooms")] = bed_counts.get(u.get("bedrooms"), 0) + 1
        if u.get("rent") is not None:
            rents.append(u["rent"])

    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "total_units": len(all_units),
        "stale_after_hours": STALE_AFTER_HOURS,
        "sources": sorted(sources_meta, key=lambda s: -s["unit_count"]),
        "facets": {
            "cities": sorted(
                ({"name": c, "count": n} for c, n in city_counts.items()),
                key=lambda x: (-x["count"], x["name"]),
            ),
            "bedrooms": sorted(
                (
                    {"value": b, "label": _bedroom_label(b), "count": n}
                    for b, n in bed_counts.items()
                ),
                key=lambda x: (x["value"] is None, x["value"] or 0),
            ),
            "rent": {
                "min": min(rents) if rents else None,
                "max": max(rents) if rents else None,
            },
            "kinds": sorted(
                ({"name": k, "count": n} for k, n in kind_counts.items()),
                key=lambda x: -x["count"],
            ),
        },
        # Non-livable listings dropped from the dashboard, reported so the
        # count is auditable rather than a silent filter.
        "excluded": dict(sorted(excluded_kinds.items())),
        "units": all_units,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(
        f"wrote {OUT.relative_to(REPO_ROOT)}: {len(all_units)} units "
        f"from {len(sources_meta)} sources across {len(city_counts)} cities"
    )
    if excluded_kinds:
        detail = ", ".join(f"{k}={n}" for k, n in sorted(excluded_kinds.items()))
        print(f"  excluded {sum(excluded_kinds.values())} non-residential listings ({detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
