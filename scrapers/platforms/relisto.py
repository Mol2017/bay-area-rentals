"""Adapter for ReListo (WordPress REST API).

ReListo publishes listings as a WordPress custom post type exposed at
``/wp-json/wp/v2/listings``, which returns clean JSON -- no HTML parsing.

Two quirks drive the shape of this module:

  * **The archive is enormous and mostly dead.** ~1700 records exist, but the
    vast majority are ``leased`` or ``offmarket`` history. Only records with
    ``meta.property_status == "current"`` are actually available, so the
    status filter is not an optimization -- without it the dashboard would
    show a thousand-plus units that cannot be rented.
  * **Server-side meta filtering is silently ignored.** Passing
    ``?property_status=current`` returns the unfiltered page anyway, so we
    must page through everything and filter client-side. ``_fields`` trims
    the payload from ~2.3 MB to ~520 KB per page, which makes that
    affordable.
"""
from __future__ import annotations

import json

from base import (
    Unit,
    classify_listing,
    infer_bedrooms_from_title,
    parse_available,
    parse_bedrooms,
    parse_rent,
    parse_square_feet,
    polite_get,
)

PLATFORM = "relisto"

BASE = "https://www.relisto.com"
API = f"{BASE}/wp-json/wp/v2/listings"
PER_PAGE = 100
# Backstop so a runaway pagination bug can't loop forever; the archive is
# ~17 pages, so 40 is generous headroom without being unbounded.
MAX_PAGES = 40

AVAILABLE_STATUS = "current"


def _fetch_page(page: int) -> list[dict]:
    url = (
        f"{API}?per_page={PER_PAGE}&page={page}"
        "&_fields=id,link,title,meta"
    )
    return json.loads(polite_get(url, headers={"Accept": "application/json"}))


def _build_address(meta: dict) -> str | None:
    """Assemble a street address from ReListo's split fields."""
    full = (meta.get("map") or {}).get("address") if isinstance(meta.get("map"), dict) else None
    if full:
        return str(full).strip()

    parts = [
        str(meta.get("street_number") or "").strip(),
        str(meta.get("street_name") or "").strip(),
    ]
    street = " ".join(p for p in parts if p).strip()
    unit = str(meta.get("unit") or "").strip()
    if unit:
        street = f"{street} #{unit}" if street else f"#{unit}"
    if not street:
        return None

    tail = ", ".join(
        p for p in [str(meta.get("city") or "").strip(),
                    str(meta.get("state") or "").strip()] if p
    )
    zipc = str(meta.get("zip_code") or "").strip()
    if tail and zipc:
        tail = f"{tail} {zipc}"
    return f"{street}, {tail}" if tail else street


def scrape(*, source: str, manager: str, **_ignored) -> list[Unit]:
    units: list[Unit] = []

    for page in range(1, MAX_PAGES + 1):
        try:
            records = _fetch_page(page)
        except Exception as e:  # noqa: BLE001
            # WordPress returns 400 once you page past the end; that's the
            # normal termination condition, not a failure.
            if page == 1:
                raise
            print(f"  [relisto] stopped at page {page}: {e}")
            break

        if not records:
            break

        for rec in records:
            meta = rec.get("meta") or {}
            if str(meta.get("property_status") or "").lower() != AVAILABLE_STATUS:
                continue

            city = (meta.get("city") or "").strip() or None
            if not city:
                # The archive contains corrupt rows with every address field
                # null; they carry no usable information.
                continue

            address = _build_address(meta)
            if not address:
                continue

            title = (rec.get("title") or {}).get("rendered") if isinstance(
                rec.get("title"), dict
            ) else None

            # Empty bedrooms means an SRO / single room, which classify_listing
            # picks up from the title.
            beds = parse_bedrooms(meta.get("bedrooms"))
            if beds is None:
                beds = infer_bedrooms_from_title(title)

            # meta.availability_date is "YYYY/MM/DD"; parse_available reads
            # the numeric form once the separators are normalized.
            raw_date = str(meta.get("availability_date") or "").replace("/", "-")
            avail_date, avail_now = parse_available(raw_date)

            units.append(
                Unit(
                    address=address,
                    source=source,
                    manager=manager,
                    city=city,
                    state=(meta.get("state") or "CA").strip() or "CA",
                    postal_code=(str(meta.get("zip_code") or "").strip() or None),
                    kind=classify_listing(address, title, beds),
                    bedrooms=beds,
                    rent=parse_rent(meta.get("price")),
                    available_date=avail_date,
                    available_now=avail_now,
                    square_feet=parse_square_feet(meta.get("square_feet")),
                    title=title,
                    url=rec.get("link") or BASE,
                )
            )

        if len(records) < PER_PAGE:
            break

    return units
