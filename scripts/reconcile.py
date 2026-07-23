#!/usr/bin/env python3
"""Apply the detail-page reading over the index parser where they disagree.

Runs after ``enrich_all.py`` and before ``merge.py``. The parser reads a
manager's *index* page; the model reads each listing's own *detail* page.
When they conflict it is almost always because the detail page simply has
more -- a stated year the index truncated to "5/15", a per-room floor plan
the index reported as the unit's own bedroom count, an office suite the
classifier had no keyword for. So the detail page wins, per field, with two
deliberate exceptions.

`data/conflicts.json`, written by the enrichment pass, is the input. Every
change made here is echoed to stdout, because moving a headline field is not
something that should happen silently.

Exceptions -- fields the parser keeps even when the page differs:

* **rent.** The parser deliberately reports the low end of an advertised
  range, which a detail-page reader will often disagree with by quoting the
  headline number instead. That is a definitional difference, not an error,
  and overriding it would break the "filter on the entry price" contract the
  whole rent column is built on. Rent conflicts are left for review.

* **square_feet.** Small enough drift to be rounding ("about 900" vs 890),
  large enough to be a different unit on a shared page. Not worth trusting
  either reader blindly; left for review.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from schema import RAW_DIR, UNIT_TYPES, derive_unit_type  # noqa: E402

CONFLICTS = REPO_ROOT / "data" / "conflicts.json"

# Fields the detail page is authoritative on. rent and square_feet are
# excluded on purpose -- see the module docstring.
APPLY_FIELDS = {"kind", "bedrooms", "bathrooms", "available_date", "available_now"}


def main() -> int:
    if not CONFLICTS.exists():
        print(f"no {CONFLICTS.name}; run enrich_all.py first", file=sys.stderr)
        return 1
    report = json.loads(CONFLICTS.read_text())
    rows = report.get("rows", [])

    # Index conflicts by (url, field) so a unit can be matched to its
    # resolution without an address join, which shared pages would break.
    by_url: dict[str, dict[str, object]] = {}
    for r in rows:
        if r["field"] in APPLY_FIELDS:
            by_url.setdefault(r["url"], {})[r["field"]] = r["page"]

    applied = Counter()
    skipped = Counter()
    for path in sorted(RAW_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        doc = json.loads(path.read_text())
        changed = False
        for u in doc.get("units", []):
            fixes = by_url.get(u.get("url"))
            if not fixes:
                continue
            for field, page_value in fixes.items():
                if page_value is None or u.get(field) == page_value:
                    continue
                old = u.get(field)
                u[field] = page_value
                changed = True
                applied[field] += 1
                # bedrooms drives unit_type, which the parser derived from the
                # now-stale value; recompute it here.
                if field == "bedrooms":
                    ut = derive_unit_type(page_value)
                    # reconcile writes raw dicts, bypassing Unit.validate --
                    # so guard the one derived field here rather than trust it.
                    if ut is not None and ut not in UNIT_TYPES:
                        raise AssertionError(f"derive_unit_type({page_value}) "
                                             f"-> invalid {ut!r}")
                    u["unit_type"] = ut
                print(f"  {field:15} {str(old):>12} -> {str(page_value):<12} "
                      f"{(u.get('address') or '')[:44]}")
        if changed:
            path.write_text(json.dumps(doc, indent=2))

    for r in rows:
        if r["field"] not in APPLY_FIELDS:
            skipped[r["field"]] += 1

    print(f"\napplied {sum(applied.values())} detail-page values over the parser:")
    for field, n in applied.most_common():
        print(f"  {field:15} {n}")
    if skipped:
        detail = ", ".join(f"{f}={n}" for f, n in sorted(skipped.items()))
        print(f"left for review (parser kept): {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
