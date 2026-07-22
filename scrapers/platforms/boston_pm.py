"""Adapter for Boston Property Management (WordPress "Yelling Mule" theme).

Boston PM runs RentManager internally, but like Premium Properties its
RentManager portals are login-only. The public inventory lives on the
WordPress site as a ``resident_listings`` custom post type rendered at
``/residents/listings/``.

Each listing is a ``div.ym_listing`` block::

    <div class="ym_listing">
      <h4><a href="/resident_listings/10-merrymount-301/">10 Merrymount #301</a></h4>
      <h5>$2,700 &ndash; 2 Bed-1Bath</h5>
      <h5>Availability: Now</h5>

Two things make this source more work than it looks:

  * **City is not a field.** It is embedded in two places instead: the title
    suffix ("10 Merrymount # 403 - Quincy") and the detail-page slug
    ("/resident_listings/10-merrymount-403-quincy/"). We derive a city
    vocabulary from the title suffixes across the whole page, then use it to
    validate slug tails -- which stops a street name like "merrymount" from
    being mistaken for a city. Anything still unresolved is matched against
    another listing on the same street. This resolves every card without the
    six extra requests that iterating the ``?city=`` filter would cost.
  * **The card collapses every past date to "Now".** A unit that became
    available in February 2025 still reads "Availability: Now", which is
    accurate for the renter, so we take it at face value and set
    ``available_now`` rather than fetching each detail page for a historical
    date that does not change the answer.
"""
from __future__ import annotations

import re

from base import (
    Unit,
    classify_listing,
    clean_text,
    normalize_city,
    parse_available,
    parse_bathrooms,
    parse_bedrooms,
    parse_rent,
    polite_get,
)

PLATFORM = "boston_pm"

BASE = "https://bostonpropertymanagementllc.com"
LISTING_URL = f"{BASE}/residents/listings/"

_BLOCK_SPLIT_RE = re.compile(r'<div[^>]*class="[^"]*\bym_listing\b', re.I)
_TITLE_RE = re.compile(
    r"<h4[^>]*>\s*(?:<a[^>]*href=\"([^\"]*)\"[^>]*>)?(.*?)(?:</a>)?\s*</h4>", re.I | re.S
)
_H5_RE = re.compile(r"<h5[^>]*>(.*?)</h5>", re.I | re.S)

# "10 Merrymount # 403 - Quincy" -- the separator is an en dash in the source,
# which clean_text has already normalized to a hyphen by this point.
_TITLE_CITY_RE = re.compile(r"[-–—]\s*([A-Za-z][A-Za-z .'-]{2,30})\s*$")
# Trailing WordPress dedupe counter on a slug: "...-chelsea-2"
_SLUG_DEDUPE_RE = re.compile(r"-\d+$")
# Leading street number + name, used to match a city-less listing to a
# sibling on the same street.
_STREET_KEY_RE = re.compile(r"^\s*([\d\-]+\s+[A-Za-z][A-Za-z .'-]*)")


def _title_city(title: str) -> str | None:
    m = _TITLE_CITY_RE.search(title)
    if not m:
        return None
    candidate = m.group(1).strip()
    # Guard against a title ending in a unit descriptor rather than a city.
    if re.fullmatch(r"(?:apt|unit|suite|ste|bed|bath|studio)\.?", candidate, re.I):
        return None
    return normalize_city(candidate)


def _slug_tail(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"/resident_listings/([a-z0-9\-]+)/?", href, re.I)
    if not m:
        return None
    slug = _SLUG_DEDUPE_RE.sub("", m.group(1).lower())
    tail = slug.rsplit("-", 1)[-1]
    return tail or None


def _street_key(address: str) -> str | None:
    m = _STREET_KEY_RE.match(address)
    return m.group(1).lower().strip() if m else None


def parse_listings(html: str, *, source: str, manager: str) -> list[Unit]:
    """Parse the listings index, resolving each card's city in two passes."""
    raw: list[dict] = []

    for block in _BLOCK_SPLIT_RE.split(html)[1:]:
        title_m = _TITLE_RE.search(block)
        if not title_m:
            continue
        href = title_m.group(1)
        title = clean_text(title_m.group(2))
        if not title:
            continue
        h5s = [clean_text(h) for h in _H5_RE.findall(block)]
        raw.append({"href": href, "title": title, "h5s": h5s})

    # Pass 1 -- build the city vocabulary from title suffixes, plus a
    # street -> city map for the cards that do name their city.
    vocabulary: set[str] = set()
    street_city: dict[str, str] = {}
    for r in raw:
        city = _title_city(r["title"])
        r["city"] = city
        if city:
            vocabulary.add(city.lower())
            key = _street_key(r["title"])
            if key:
                street_city.setdefault(key, city)

    # Pass 2 -- fill the gaps from the slug tail (validated against the
    # vocabulary so a street name can't pose as a city), then from a sibling
    # listing on the same street.
    for r in raw:
        if r["city"]:
            continue
        tail = _slug_tail(r["href"])
        if tail and tail in vocabulary:
            r["city"] = normalize_city(tail)
            continue
        key = _street_key(r["title"])
        if key and key in street_city:
            r["city"] = street_city[key]

    units: list[Unit] = []
    for r in raw:
        combined = " ".join(r["h5s"])
        avail_text = next((h for h in r["h5s"] if re.search(r"avail", h, re.I)), "")
        avail_date, avail_now = parse_available(
            re.sub(r"^\s*availability\s*:?\s*", "", avail_text, flags=re.I)
        )

        beds = parse_bedrooms(combined)
        city = r["city"]

        # Strip the " - Quincy" suffix so the address doesn't repeat the city.
        street = _TITLE_CITY_RE.sub("", r["title"]).strip() if city else r["title"]
        full_address = f"{street}, {city}, MA" if city else street

        href = r["href"]
        units.append(
            Unit(
                address=full_address,
                source=source,
                manager=manager,
                city=city,
                # Boston PM is the one non-California source in the registry.
                state="MA",
                kind=classify_listing(full_address, combined, beds),
                bedrooms=beds,
                bathrooms=parse_bathrooms(combined),
                rent=parse_rent(combined),
                available_date=avail_date,
                available_now=avail_now,
                title=street,
                url=(
                    f"{BASE}{href}" if href and href.startswith("/") else (href or LISTING_URL)
                ),
            )
        )

    return units


def scrape(*, source: str, manager: str, **_ignored) -> list[Unit]:
    html = polite_get(LISTING_URL)
    return parse_listings(html, source=source, manager=manager)
