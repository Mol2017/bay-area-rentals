"""Adapter for Buildium-hosted listing portals (``*.managebuilding.com``).

Buildium serves every account's public rentals page at
``https://<account>.managebuilding.com/Resident/public/rentals`` with the same
server-rendered markup. No JavaScript rendering is required.

Buildium is the friendlier of the two big platforms: each listing card is an
``<a>`` carrying machine-readable ``data-*`` attributes, so bedrooms, rent and
city come from structured values rather than prose::

    <a class="featured-listing" href="/Resident/public/rentals/44351"
       data-bedrooms="2" data-bathrooms="1" data-rent="1200.00"
       data-square-feet="729" data-location="Fresno,CA|93726">
      <h3 class="featured-listing__title">1919 E. Sussex Way, 112</h3>
      <p class="featured-listing__address">Fresno, CA 93726</p>
      <p class="featured-listing__price accent-color">$1,200</p>
      <p class="featured-listing__availability">available September 1</p>

We prefer the ``data-*`` attributes and fall back to the rendered text, which
matters because the attributes are absent on a minority of cards.

Note that ``data-location`` gives the city directly, so unlike AppFolio we
never have to infer it from a street address -- but the availability string
("available September 1") carries no year, which is exactly the case
``parse_available`` handles with its year-inference grace window.
"""
from __future__ import annotations

import re

from base import (
    Unit,
    classify_listing,
    clean_text,
    infer_bedrooms_from_title,
    parse_available,
    parse_bathrooms,
    parse_bedrooms,
    parse_location,
    parse_rent,
    parse_square_feet,
    polite_get,
)

PLATFORM = "buildium"

_ITEM_SPLIT_RE = re.compile(r'<a[^>]*class="[^"]*featured-listing[^"]*"', re.I)

_TITLE_RE = re.compile(
    r'class="[^"]*featured-listing__title[^"]*"[^>]*>(.*?)</h3>', re.I | re.S
)
_ADDRESS_RE = re.compile(
    r'class="[^"]*featured-listing__address[^"]*"[^>]*>(.*?)</p>', re.I | re.S
)
_PRICE_RE = re.compile(
    r'class="[^"]*featured-listing__price[^"]*"[^>]*>(.*?)</p>', re.I | re.S
)
_AVAIL_RE = re.compile(
    r'class="[^"]*featured-listing__availability[^"]*"[^>]*>(.*?)</p>', re.I | re.S
)
_FEATURES_RE = re.compile(
    r'class="[^"]*featured-listing__features[^"]*"[^>]*>(.*?)</p>', re.I | re.S
)
_HREF_RE = re.compile(r'href="(/Resident/public/rentals/[^"]+)"', re.I)
_IMG_RE = re.compile(r'<img[^>]*src="([^"]+)"[^>]*class="[^"]*featured-listing__image', re.I)


def _attr(name: str, block: str) -> str | None:
    m = re.search(rf'{name}="([^"]*)"', block, re.I)
    val = (m.group(1).strip() if m else "")
    return val or None


def _first(pattern: re.Pattern, block: str) -> str | None:
    m = pattern.search(block)
    return clean_text(m.group(1)) if m else None


def _coalesce(*values):
    """First value that is not None.

    Deliberately not ``a or b``: a studio parses to ``0.0`` and a free unit
    would parse to ``0`` -- both falsy, both correct answers we must not
    discard in favour of a fallback.
    """
    for v in values:
        if v is not None:
            return v
    return None


def parse_listings(html: str, *, source: str, manager: str, base_url: str) -> list[Unit]:
    """Parse a Buildium public rentals page into Units."""
    units: list[Unit] = []

    for block in _ITEM_SPLIT_RE.split(html)[1:]:
        title = _first(_TITLE_RE, block)
        address_line = _first(_ADDRESS_RE, block)

        # The title is the street address ("1919 E. Sussex Way, 112") and the
        # address line is the city/state/ZIP. Together they form the full
        # address; the title alone is what distinguishes units in a building.
        if title and address_line:
            address = f"{title}, {address_line}"
        else:
            address = title or address_line
        if not address:
            continue

        features = _first(_FEATURES_RE, block)  # "2 Bed | 1 Bath | 729 sqft"

        # data-location ("Fresno,CA|93726") is authoritative; the rendered
        # address line is the fallback.
        city, state, postal = parse_location(_attr("data-location", block))
        if not city:
            city, state, postal = parse_location(address_line or address)

        avail_date, avail_now = parse_available(_first(_AVAIL_RE, block))

        href_m = _HREF_RE.search(block)
        img_m = _IMG_RE.search(block)
        img = img_m.group(1) if img_m else None
        if img and img.startswith("/"):
            img = f"{base_url.rstrip('/')}{img}"

        bedrooms = _coalesce(
            parse_bedrooms(_attr("data-bedrooms", block)),
            parse_bedrooms(features),
            infer_bedrooms_from_title(title),
        )

        units.append(
            Unit(
                address=address,
                source=source,
                manager=manager,
                city=city,
                state=state or "CA",
                postal_code=postal,
                kind=classify_listing(address, title, bedrooms),
                bedrooms=bedrooms,
                bathrooms=_coalesce(
                    parse_bathrooms(_attr("data-bathrooms", block)),
                    parse_bathrooms(features),
                ),
                rent=_coalesce(
                    parse_rent(_attr("data-rent", block)),
                    parse_rent(_first(_PRICE_RE, block)),
                ),
                available_date=avail_date,
                available_now=avail_now,
                square_feet=_coalesce(
                    parse_square_feet(_attr("data-square-feet", block)),
                    parse_square_feet(features),
                ),
                title=title,
                url=f"{base_url.rstrip('/')}{href_m.group(1)}" if href_m else base_url,
            )
        )

    return units


def scrape(*, source: str, manager: str, account: str, **_ignored) -> list[Unit]:
    """Fetch and parse one Buildium account's public rentals page."""
    base_url = f"https://{account}.managebuilding.com"
    html = polite_get(f"{base_url}/Resident/public/rentals")
    return parse_listings(html, source=source, manager=manager, base_url=base_url)
