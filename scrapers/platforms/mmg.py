"""Adapter for MMG Properties (WordPress + Realtyna WPL Pro).

``https://mmgprop.com/low-to-high/`` renders the whole portfolio in one
response (page size 48 against ~38 listings), so there is no pagination to
follow. The various ``/san-francisco/``, ``/east-bay/`` and
``/studio-apartment-rentals/`` pages are the same widget with client-side
show/hide filters over already-rendered markup -- scraping the unfiltered
page once gets everything.

Each listing is a ``div.wpl_prp_cont`` block::

    <h3 class="wpl_prp_title">...</h3>
    <h4 class="wpl_prp_listing_location">957 Mission Street, San Francisco,
        California, United States 94103</h4>
    <div class="bedroom"><span class="value">1</span></div>
    <div class="price_box"><span>$1,400</span></div>

**MMG publishes no availability date.** Not on the listing page and not on
the detail page, whose "Basic Details" block lists Property Type, Listing
Type, Price, Bedrooms, Bathrooms and Days On Market but no move-in field.
Units from this source therefore carry ``available_date=None``, which the
schema permits and the dashboard renders as "Not listed". That is a real gap
in the source rather than a parser shortcoming -- please don't "fix" it by
inventing a date.

Studios omit the ``div.bedroom`` element entirely rather than setting it to
zero, so a missing bedroom element is interpreted as a studio when the title
corroborates it.
"""
from __future__ import annotations

import re

from base import (
    Unit,
    classify_listing,
    clean_text,
    infer_bedrooms_from_title,
    parse_bedrooms,
    normalize_city,
    parse_location,
    parse_rent,
    parse_square_feet,
    polite_get,
)

PLATFORM = "mmg"

BASE = "https://mmgprop.com"
LISTING_URL = f"{BASE}/low-to-high/"

_BLOCK_SPLIT_RE = re.compile(r'<div[^>]*class="[^"]*wpl_prp_cont', re.I)
_TITLE_RE = re.compile(r'class="[^"]*wpl_prp_title[^"]*"[^>]*>(.*?)</h3>', re.I | re.S)
_LOCATION_RE = re.compile(
    r'class="[^"]*wpl_prp_listing_location[^"]*"[^>]*>(.*?)</h4>', re.I | re.S
)
_BEDROOM_RE = re.compile(
    r'class="[^"]*\bbedroom\b[^"]*"[^>]*>.*?class="[^"]*\bvalue\b[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S,
)
_BATHROOM_RE = re.compile(
    r'class="[^"]*\bbathroom\b[^"]*"[^>]*>.*?class="[^"]*\bvalue\b[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S,
)
_PRICE_RE = re.compile(
    r'class="[^"]*price_box[^"]*"[^>]*>\s*<span[^>]*>(.*?)</span>', re.I | re.S
)
_LIVING_AREA_RE = re.compile(
    r'class="[^"]*living_area[^"]*"[^>]*>.*?class="[^"]*\bvalue\b[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S,
)
_HREF_RE = re.compile(r'href="(https?://mmgprop\.com/properties/[^"]+)"', re.I)


def _city_from_wpl_location(text: str | None) -> tuple[str | None, str | None]:
    """Parse Realtyna's location line into ``(city, postal_code)``.

    The format is ``"957 Mission Street, San Francisco, California, United
    States 94103"`` -- comma field 1 is the city, and the ZIP trails the
    country. This does not match a normal US address layout, so
    ``parse_location`` cannot handle it directly.
    """
    if not text:
        return None, None
    parts = [p.strip() for p in clean_text(text).split(",")]
    city = normalize_city(parts[1]) if len(parts) > 1 else None
    zip_m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", clean_text(text))
    return city, (zip_m.group(1) if zip_m else None)


def parse_listings(html: str, *, source: str, manager: str) -> list[Unit]:
    units: list[Unit] = []

    for block in _BLOCK_SPLIT_RE.split(html)[1:]:
        loc_m = _LOCATION_RE.search(block)
        location = clean_text(loc_m.group(1)) if loc_m else None
        title_m = _TITLE_RE.search(block)
        title = clean_text(title_m.group(1)) if title_m else None

        address = location or title
        if not address:
            continue

        city, postal = _city_from_wpl_location(location)
        if not city:
            city = parse_location(address)[0]

        bed_m = _BEDROOM_RE.search(block)
        bath_m = _BATHROOM_RE.search(block)

        beds = parse_bedrooms(clean_text(bed_m.group(1))) if bed_m else None
        if beds is None:
            beds = infer_bedrooms_from_title(title)
        if beds is None and bath_m:
            # Realtyna WPL renders the bedroom element only when the count is
            # non-zero, so a card that rendered its bathroom element but no
            # bedroom element is a studio. Requiring the bathroom element is
            # what distinguishes "zero bedrooms" from "this block didn't
            # render its detail fields at all". Most such cards also say
            # "studio" in their own copy, and the detail pages confirm
            # "Listing Type: Studio".
            beds = 0.0
        price_m = _PRICE_RE.search(block)
        area_m = _LIVING_AREA_RE.search(block)
        href_m = _HREF_RE.search(block)

        units.append(
            Unit(
                address=address,
                source=source,
                manager=manager,
                city=city,
                state="CA",
                postal_code=postal,
                kind=classify_listing(address, title, beds),
                bedrooms=beds,
                bathrooms=parse_bedrooms(clean_text(bath_m.group(1))) if bath_m else None,
                rent=parse_rent(price_m.group(1)) if price_m else None,
                # See module docstring: the source publishes no move-in date.
                available_date=None,
                available_now=False,
                square_feet=parse_square_feet(clean_text(area_m.group(1))) if area_m else None,
                title=title,
                url=href_m.group(1) if href_m else LISTING_URL,
                notes="Move-in date not published by this source",
            )
        )

    return units


def scrape(*, source: str, manager: str, **_ignored) -> list[Unit]:
    html = polite_get(LISTING_URL)
    return parse_listings(html, source=source, manager=manager)
