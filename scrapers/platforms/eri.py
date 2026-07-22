"""Adapter for ERI Property Management (hand-rolled PHP site).

``https://www.erirentals.com/rental.php`` is a bespoke PHP page with no CMS
and no third-party portal behind it. The listing text is server-rendered --
only the photos are injected by jQuery -- so plain HTTP is enough.

Each unit is a ``div.rental3`` block::

    <div class="rental3">
      <p class="rraddr"><a href="unit.php?id=123">1717 Blake St</a></p>
      <p class="rrunitid">Unit D</p>
      <p class="rrcity">Berkeley</p>
      <p class="rr">2 bed 1 bath&nbsp;&bull;&nbsp;$2,450</p>
      <p class="rr">Available: Now</p>

ERI is a small operator -- expect a couple of units, not dozens. A low count
here is their real inventory rather than a broken parse, which is worth
knowing before someone "fixes" this scraper for returning too little.
"""
from __future__ import annotations

import re

from base import (
    Unit,
    classify_listing,
    clean_text,
    parse_available,
    parse_bathrooms,
    parse_bedrooms,
    parse_rent,
    polite_get,
)

PLATFORM = "eri"

BASE = "https://www.erirentals.com"
LISTING_URL = f"{BASE}/rental.php"

_BLOCK_SPLIT_RE = re.compile(r'<div[^>]*class="rental3', re.I)
_ADDR_RE = re.compile(
    r'class="rraddr"[^>]*>\s*(?:<a[^>]*href="(unit\.php\?id=\d+)"[^>]*>)?(.*?)(?:</a>)?\s*</p>',
    re.I | re.S,
)
_CITY_RE = re.compile(r'class="rrcity"[^>]*>(.*?)</p>', re.I | re.S)
_UNIT_RE = re.compile(r'class="rrunitid"[^>]*>(.*?)</p>', re.I | re.S)
_RR_RE = re.compile(r'class="rr"[^>]*>(.*?)</p>', re.I | re.S)


def parse_listings(html: str, *, source: str, manager: str) -> list[Unit]:
    """Parse ERI's rental.php into Units. Split out for fixture testing."""
    units: list[Unit] = []

    for block in _BLOCK_SPLIT_RE.split(html)[1:]:
        addr_m = _ADDR_RE.search(block)
        if not addr_m:
            continue
        street = clean_text(addr_m.group(2))
        if not street:
            continue

        href = addr_m.group(1)
        city_m = _CITY_RE.search(block)
        unit_m = _UNIT_RE.search(block)
        city = clean_text(city_m.group(1)) if city_m else None
        unit_no = clean_text(unit_m.group(1)) if unit_m else None

        address = f"{street}, {unit_no}" if unit_no else street
        if city:
            address = f"{address}, {city}, CA"

        # The `rr` paragraphs carry, in order, the bed/bath + price line and
        # the availability line. Concatenating them and letting each parser
        # find its own pattern is more robust than trusting the order.
        rr_texts = [clean_text(t) for t in _RR_RE.findall(block)]
        combined = " ".join(rr_texts)

        avail_text = next(
            (t for t in rr_texts if re.search(r"avail", t, re.I)), combined
        )
        avail_date, avail_now = parse_available(
            re.sub(r"^\s*available\s*:?\s*", "", avail_text, flags=re.I)
        )

        beds = parse_bedrooms(combined)

        units.append(
            Unit(
                address=address,
                source=source,
                manager=manager,
                city=city,
                state="CA",
                kind=classify_listing(address, combined, beds),
                bedrooms=beds,
                bathrooms=parse_bathrooms(combined),
                rent=parse_rent(combined),
                available_date=avail_date,
                available_now=avail_now,
                url=f"{BASE}/{href}" if href else LISTING_URL,
            )
        )

    return units


def scrape(*, source: str, manager: str, **_ignored) -> list[Unit]:
    html = polite_get(LISTING_URL)
    return parse_listings(html, source=source, manager=manager)
