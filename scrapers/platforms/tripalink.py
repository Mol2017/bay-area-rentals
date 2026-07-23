"""Adapter for Tripalink's Berkeley portfolio.

Tripalink runs a Next.js app that exposes its own page data as JSON through
the ``_next/data`` endpoint, so no HTML parsing and no browser are needed --
but it takes two hops, because the property-list response carries only a
*range* of bedrooms and a starting price, while the per-unit facts we need
live on each property's detail response.

    1. GET the listing page, read ``buildId`` out of the ``__NEXT_DATA__``
       script tag. This changes on every Tripalink deploy, so it must be read
       fresh each run -- a hardcoded buildId 404s within days.
    2. GET ``/_next/data/<buildId>/berkeley/homes-for-rent.json`` for the
       property list.
    3. GET ``/_next/data/<buildId>/apartments/bay-area/berkeley/<id>.json``
       per property, and read ``pageProps.data.availableUnits[]``.

Tripalink is a co-living operator, which drives the one subtlety here: most
listings are a single room inside a shared apartment, so a "3 bd" unit at
$810 is one bedroom in a 3-bedroom flat, not a whole 3-bedroom for $810.
Their ``rentWhole`` field reads ``"1"`` on those per-room listings too and so
cannot be used to tell them apart -- the reliable signal is a ``rooms`` array
of length 1 against a multi-bedroom floor plan. Those are tagged
``kind="room"`` so they can be filtered out of whole-apartment searches
instead of polluting the bedroom facet.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from base import (
    KIND_ROOM,
    PACIFIC,
    Unit,
    classify_listing,
    parse_available,
    parse_bathrooms,
    parse_bedrooms,
    parse_rent,
    parse_square_feet,
    polite_get,
)
from enrich import pets_from_policy, room_type_from_occupancy

PLATFORM = "tripalink"

BASE = "https://tripalink.com"
LISTING_PAGE = f"{BASE}/berkeley/homes-for-rent"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S,
)


def _get_build_id() -> str:
    """Read the current Next.js buildId from the listing page."""
    html = polite_get(LISTING_PAGE)
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("tripalink: __NEXT_DATA__ script tag not found")
    build_id = json.loads(m.group(1)).get("buildId")
    if not build_id:
        raise RuntimeError("tripalink: buildId missing from __NEXT_DATA__")
    return build_id


def _get_json(url: str) -> dict:
    return json.loads(polite_get(url, headers={"Accept": "application/json"}))


def _epoch_ms_to_iso(value) -> str | None:
    """Convert Tripalink's millisecond epoch availability stamp to ISO date."""
    if value in (None, "", 0):
        return None
    try:
        ts = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(ts, tz=PACIFIC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _units_from_property(prop: dict, *, source: str, manager: str) -> list[Unit]:
    address = prop.get("address") or prop.get("name")
    if not address:
        return []

    city = prop.get("city") or "Berkeley"
    prop_id = prop.get("id")
    url = f"{BASE}/apartments/bay-area/berkeley/{prop_id}" if prop_id else LISTING_PAGE

    # Property-level policy, already in this payload -- no extra request and
    # no model needed. Covers 259 of the dataset's 306 room listings, which
    # is why the enrichment pass barely touches Tripalink.
    lease_policy = prop.get("leasePolicy")
    pets, _ = pets_from_policy(lease_policy)

    out: list[Unit] = []
    for u in prop.get("availableUnits") or []:
        beds = parse_bedrooms(u.get("floorPlanBedroomNum"))
        rent = parse_rent(u.get("minPrice")) or parse_rent(u.get("maxPrice"))

        # availableStartTime is authoritative; availableContent ("Available
        # from 08/14/2026") is the human-readable fallback.
        iso = _epoch_ms_to_iso(u.get("availableStartTime"))
        now = False
        if iso is None:
            iso, now = parse_available(u.get("availableContent"))
        if iso is None and u.get("availableNow"):
            now = True

        unit_no = u.get("name")
        full_address = f"{address} #{unit_no}" if unit_no else address

        # Tripalink's `rentWhole` flag reads "1" even on per-room listings,
        # so it cannot be trusted. The reliable signal is the `rooms` array:
        # a single room offered inside a multi-bedroom floor plan means the
        # advertised price buys that one room, not the whole apartment.
        rooms = u.get("rooms") or []
        per_room = len(rooms) == 1 and beds is not None and beds >= 2

        kind = KIND_ROOM if per_room else classify_listing(
            full_address, u.get("unitType"), beds
        )
        # Only a 1-tenant cap resolves this for free. A 2-tenant cap leaves
        # open whether the second person is your partner or a stranger the
        # operator matched you with, so it stays None for the enrichment
        # pass to attempt -- see enrich.room_type_from_occupancy.
        room_type, _ = room_type_from_occupancy(
            lease_policy, is_room=(kind == KIND_ROOM)
        )

        out.append(
            Unit(
                address=full_address,
                source=source,
                manager=manager,
                city=city,
                state="CA",
                kind=kind,
                room_type=room_type,
                pets=pets,
                bedrooms=beds,
                bathrooms=parse_bathrooms(u.get("floorPlanBathroomNum")),
                rent=rent,
                available_date=iso,
                available_now=now,
                square_feet=parse_square_feet(u.get("totalArea")),
                title=prop.get("name"),
                url=url,
            )
        )
    return out


def scrape(*, source: str, manager: str, **_ignored) -> list[Unit]:
    build_id = _get_build_id()

    list_url = (
        f"{BASE}/_next/data/{build_id}/berkeley/homes-for-rent.json"
        "?urlPath=berkeley&urlPath=homes-for-rent"
    )
    doc = _get_json(list_url)
    properties = (
        doc.get("pageProps", {}).get("data", {}).get("ESProperties", {}).get("data")
        or []
    )

    units: list[Unit] = []
    for prop in properties:
        prop_id = prop.get("id")
        if not prop_id:
            continue
        detail_url = (
            f"{BASE}/_next/data/{build_id}/apartments/bay-area/berkeley/{prop_id}.json"
            f"?cityRoute=bay-area&areaRoute=berkeley&id={prop_id}"
        )
        try:
            detail = _get_json(detail_url)
        except Exception as e:  # noqa: BLE001  one dead property must not kill the run
            print(f"  [tripalink] property {prop_id} detail failed: {e}")
            continue
        data = detail.get("pageProps", {}).get("data") or {}
        if not data:
            continue
        units.extend(_units_from_property(data, source=source, manager=manager))

    return units
