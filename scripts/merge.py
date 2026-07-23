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
CONTACTS_PATH = REPO_ROOT / "data" / "contacts.json"

sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from base import LIVABLE_KINDS  # noqa: E402
from regions import in_covered_metro
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
    out_of_area: dict[str, int] = {}

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
            # Keep only the covered metros (Bay Area + Boston/Cambridge). A
            # national operator's out-of-market listings are dropped here
            # rather than by excluding the whole manager -- see regions.py.
            if not in_covered_metro(u.get("city"), u.get("state")):
                city = u.get("city") or "(unknown)"
                out_of_area[f"{city}, {u.get('state')}"] = (
                    out_of_area.get(f"{city}, {u.get('state')}", 0) + 1
                )
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

    # Last-resort default. Applied here rather than in the adapters so that
    # anything better -- a structured field, the enrichment pass -- has
    # already had its say. A whole listing that nothing contradicted is a
    # whole unit; a room that nothing resolved stays None.
    for u in all_units:
        if u.get("room_type") is None and u.get("kind") == "residential":
            u["room_type"] = "entire_place"

    # `kind` has done its job by this point -- it gated the exclusions above.
    # It is dropped from the published payload because room_type carries the
    # part a renter cares about, and two overlapping classifications on the
    # same record invite the frontend to disagree with itself. The raw files
    # keep it as the audit trail for why a listing was excluded.
    for u in all_units:
        u.pop("kind", None)

    city_counts: dict[str, int] = {}
    bed_counts: dict[float | None, int] = {}
    rents: list[float] = []
    # Amenity facets. None is counted under "unknown" rather than dropped:
    # a filter that silently omits listings the source was quiet about would
    # make the dashboard look emptier than the market is.
    facet_counts: dict[str, dict[str, int]] = {
        f: {} for f in ("room_type", "unit_type", "pets", "furnished",
                        "parking", "laundry")
    }
    for u in all_units:
        for field, counts in facet_counts.items():
            key = u.get(field) or "unknown"
            counts[key] = counts.get(key, 0) + 1
        if u.get("city"):
            city_counts[u["city"]] = city_counts.get(u["city"], 0) + 1
        bed_counts[u.get("bedrooms")] = bed_counts.get(u.get("bedrooms"), 0) + 1
        if u.get("rent") is not None:
            rents.append(u["rent"])

    # Leasing contacts are manager-level -- the same office number appears on
    # every one of a manager's listings, so it is stored once here and joined
    # on `manager` by the frontend rather than copied onto 833 records.
    contacts: dict[str, dict] = {}
    if CONTACTS_PATH.exists():
        try:
            for slug, info in json.loads(CONTACTS_PATH.read_text()).items():
                if info.get("manager"):
                    contacts[info["manager"]] = {
                        "phone": info.get("phone"),
                        "emails": info.get("emails") or [],
                        "source_url": info.get("source_url"),
                    }
        except json.JSONDecodeError as e:
            print(f"skip contacts.json: {e}", file=sys.stderr)

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
            **{
                field: sorted(
                    ({"name": k, "count": n} for k, n in counts.items()),
                    key=lambda x: (x["name"] == "unknown", -x["count"]),
                )
                for field, counts in facet_counts.items()
            },
        },
        "managers": contacts,
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
    if contacts:
        with_phone = sum(1 for c in contacts.values() if c.get("phone"))
        with_email = sum(1 for c in contacts.values() if c.get("emails"))
        print(f"  contacts: {len(contacts)} managers "
              f"({with_phone} with phone, {with_email} with email)")
    if excluded_kinds:
        detail = ", ".join(f"{k}={n}" for k, n in sorted(excluded_kinds.items()))
        print(f"  excluded {sum(excluded_kinds.values())} non-residential listings ({detail})")
    if out_of_area:
        total = sum(out_of_area.values())
        top = ", ".join(f"{c}={n}" for c, n in
                        sorted(out_of_area.items(), key=lambda kv: -kv[1])[:8])
        print(f"  dropped {total} out-of-metro listings across "
              f"{len(out_of_area)} cities ({top})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
