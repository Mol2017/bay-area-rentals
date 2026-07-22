"""Adapter for AppFolio-hosted listing portals.

AppFolio serves every account from ``https://<account>.appfolio.com/listings``
with the same server-rendered markup, so one parser covers every AppFolio
source in ``sources.py``. No JavaScript rendering is required.

The markup exposes stable ``js-*`` hook classes that AppFolio's own frontend
uses for filtering. Those are what we key off, rather than the presentational
``listing-item__*`` classes, because the ``js-`` hooks are contractual for
AppFolio's JS and therefore far less likely to be restyled away::

    <div class="listing-item result js-listing-item" id="listing_12">
      <span class="js-listing-address">2505 Virginia St #10, Berkeley, CA 94709</span>
      <div class="js-listing-blurb-rent">$3,295</div>
      <span class="js-listing-blurb-bed-bath">2 bd / 1 ba</span>
      <dd class="detail-box__value js-listing-available">8/2/26</dd>
      <h2 class="js-listing-title"><a href="/listings/detail/<uuid>">...</a></h2>

Some fields appear twice (once in the mobile "blurb" overlay, once in the
desktop detail box). We read whichever is present, preferring the detail box
since it is the fuller form: it spells out "Available 8/2/26" where the blurb
sometimes carries only the bare rent.
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

PLATFORM = "appfolio"

# Each listing card starts at a `js-listing-item` div. Splitting on that
# marker is more robust than trying to balance nested <div>s with a regex.
_ITEM_SPLIT_RE = re.compile(r'<div[^>]*class="[^"]*js-listing-item[^"]*"', re.I)

_ADDRESS_RE = re.compile(
    r'class="[^"]*js-listing-address[^"]*"[^>]*>(.*?)</span>', re.I | re.S
)
_RENT_RE = re.compile(
    r'class="[^"]*js-listing-blurb-rent[^"]*"[^>]*>(.*?)</div>', re.I | re.S
)
_BEDBATH_RE = re.compile(
    r'class="[^"]*js-listing-blurb-bed-bath[^"]*"[^>]*>(.*?)</span>', re.I | re.S
)
_AVAILABLE_RE = re.compile(
    r'class="[^"]*js-listing-available[^"]*"[^>]*>(.*?)</(?:dd|span)>', re.I | re.S
)
_TITLE_RE = re.compile(
    r'class="[^"]*js-listing-title[^"]*"[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</h2>',
    re.I | re.S,
)
_DETAIL_HREF_RE = re.compile(r'href="(/listings/detail/[^"]+)"', re.I)
_IMAGE_RE = re.compile(r'data-original="(https://images\.cdn\.appfolio\.com/[^"]+)"', re.I)

# The desktop detail box renders each fact as a <dt>label</dt><dd>value</dd>
# pair; used as a fallback when the mobile blurb hooks are absent.
_DETAIL_ITEM_RE = re.compile(
    r'<dt[^>]*class="[^"]*detail-box__label[^"]*"[^>]*>(.*?)</dt>\s*'
    r'<dd[^>]*class="[^"]*detail-box__value[^"]*"[^>]*>(.*?)</dd>',
    re.I | re.S,
)


def _detail_facts(block: str) -> dict[str, str]:
    """Return the detail box's ``{label: value}`` pairs, lowercased keys."""
    return {
        clean_text(label).lower().strip(": "): clean_text(value)
        for label, value in _DETAIL_ITEM_RE.findall(block)
    }


def _first(pattern: re.Pattern, block: str) -> str | None:
    m = pattern.search(block)
    return clean_text(m.group(1)) if m else None


def parse_listings(html: str, *, source: str, manager: str, base_url: str) -> list[Unit]:
    """Parse an AppFolio ``/listings`` page into Units.

    Exposed separately from ``scrape`` so tests can run it against a saved
    fixture without touching the network.
    """
    units: list[Unit] = []

    # The first chunk is page chrome before any listing; drop it.
    blocks = _ITEM_SPLIT_RE.split(html)[1:]

    for block in blocks:
        address = _first(_ADDRESS_RE, block)
        if not address:
            # Without an address we can't identify or dedupe the unit, and
            # the schema rejects it. Skip rather than emit a phantom row.
            continue

        facts = _detail_facts(block)

        rent_text = facts.get("rent") or _first(_RENT_RE, block)
        bedbath_text = facts.get("bed / bath") or _first(_BEDBATH_RE, block)
        avail_text = facts.get("available") or _first(_AVAILABLE_RE, block)
        sqft_text = facts.get("square feet")

        city, state, postal = parse_location(address)
        available_date, available_now = parse_available(avail_text)

        title = _first(_TITLE_RE, block)

        # Prefer the structured bed/bath element; fall back to the title only
        # when the element is absent (some studios ship without one).
        bedrooms = parse_bedrooms(bedbath_text)
        if bedrooms is None:
            bedrooms = infer_bedrooms_from_title(title)

        href_m = _DETAIL_HREF_RE.search(block)
        img_m = _IMAGE_RE.search(block)

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
                bathrooms=parse_bathrooms(bedbath_text),
                rent=parse_rent(rent_text),
                available_date=available_date,
                available_now=available_now,
                square_feet=parse_square_feet(sqft_text),
                title=title,
                url=f"{base_url.rstrip('/')}{href_m.group(1)}" if href_m else base_url,
                image=img_m.group(1) if img_m else None,
            )
        )

    return units


def scrape(*, source: str, manager: str, account: str, **_ignored) -> list[Unit]:
    """Fetch and parse one AppFolio account's listings.

    ``account`` is the portal subdomain, e.g. ``hayesmanagement`` for
    ``https://hayesmanagement.appfolio.com``.
    """
    base_url = f"https://{account}.appfolio.com"
    html = polite_get(f"{base_url}/listings")
    return parse_listings(html, source=source, manager=manager, base_url=base_url)
