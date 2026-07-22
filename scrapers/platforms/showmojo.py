"""Adapter for ShowMojo public listing feeds.

Premium Properties runs RentManager internally, but its RentManager portals
(``*.twa.rentmanager.com``) are login-only -- every public listings path
404s or 403s. What is public is the ShowMojo feed the company links to from
its own site, which is fully server-rendered.

Each listing is a ``div.listing-info.js-listing-info`` block::

    <div class='listing-info js-listing-info'>
      <div class='listing-address-header'>2325 Woolsey Street Apt. #9</div>
      <p class='listing-city-state-zip'>Berkeley, CA 94705</p>
      <div class='rent-info'>
        <div><span class='price'>$1,995</span></div>
        <div>...</div>
        <div>Available Sep 1st</div>
      </div>
      <div class='listing-icon-wrap'><img class='icon' src='.../icons/bed-2.svg'>1</div>

Bedrooms are encoded as an icon-plus-text pair rather than a labelled field,
so we look for the wrapper whose icon filename mentions ``bed``. Listings
with no bed icon at all are single rooms (Premium markets several SRO rooms
on Telegraph Ave), which ``classify_listing`` tags from the title.
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
    parse_location,
    parse_rent,
    polite_get,
)

PLATFORM = "showmojo"

_BLOCK_SPLIT_RE = re.compile(r"<div[^>]*class=['\"][^'\"]*js-listing-info", re.I)
_ADDR_RE = re.compile(
    r"class=['\"][^'\"]*listing-address-header[^'\"]*['\"][^>]*>(.*?)</div>", re.I | re.S
)
_CSZ_RE = re.compile(
    r"class=['\"][^'\"]*listing-city-state-zip[^'\"]*['\"][^>]*>(.*?)</p>", re.I | re.S
)
_RENT_INFO_RE = re.compile(
    r"class=['\"][^'\"]*rent-info[^'\"]*['\"][^>]*>(.*?)</div>\s*</div>", re.I | re.S
)
_PRICE_RE = re.compile(r"class=['\"][^'\"]*price[^'\"]*['\"][^>]*>(.*?)</span>", re.I | re.S)
_AVAIL_RE = re.compile(r"(Available[^<]{0,40})", re.I)
_ICON_WRAP_RE = re.compile(
    r"<div[^>]*class=['\"][^'\"]*listing-icon-wrap[^'\"]*['\"][^>]*>(.*?)</div>",
    re.I | re.S,
)
_HREF_RE = re.compile(r"href=['\"](https?://showmojo\.com/[^'\"]+)['\"]", re.I)


def _icon_value(block: str, icon_word: str) -> str | None:
    """Return the text of the icon-wrap whose image filename mentions *icon_word*."""
    for wrap in _ICON_WRAP_RE.findall(block):
        if re.search(rf"icons?/[^'\"]*{icon_word}", wrap, re.I):
            text = clean_text(wrap)
            if text:
                return text
    return None


def parse_listings(html: str, *, source: str, manager: str, feed_url: str) -> list[Unit]:
    units: list[Unit] = []

    for block in _BLOCK_SPLIT_RE.split(html)[1:]:
        addr_m = _ADDR_RE.search(block)
        street = clean_text(addr_m.group(1)) if addr_m else None
        if not street:
            continue

        csz_m = _CSZ_RE.search(block)
        csz = clean_text(csz_m.group(1)) if csz_m else None
        address = f"{street}, {csz}" if csz else street
        city, state, postal = parse_location(csz or address)

        rent_info_m = _RENT_INFO_RE.search(block)
        rent_info = rent_info_m.group(1) if rent_info_m else block

        price_m = _PRICE_RE.search(rent_info) or _PRICE_RE.search(block)
        rent = parse_rent(price_m.group(1) if price_m else rent_info)

        avail_m = _AVAIL_RE.search(clean_text(rent_info)) or _AVAIL_RE.search(
            clean_text(block)
        )
        avail_date, avail_now = parse_available(avail_m.group(1) if avail_m else None)

        beds = parse_bedrooms(_icon_value(block, "bed"))
        baths = parse_bathrooms(_icon_value(block, "bath"))

        href_m = _HREF_RE.search(block)

        units.append(
            Unit(
                address=address,
                source=source,
                manager=manager,
                city=city,
                state=state or "CA",
                postal_code=postal,
                kind=classify_listing(address, street, beds),
                bedrooms=beds,
                bathrooms=baths,
                rent=rent,
                available_date=avail_date,
                available_now=avail_now,
                title=street,
                url=href_m.group(1) if href_m else feed_url,
            )
        )

    return units


def scrape(*, source: str, manager: str, account: str, **_ignored) -> list[Unit]:
    """``account`` is the ShowMojo feed id, e.g. ``a2a2489044``."""
    feed_url = f"https://showmojo.com/{account}/l"
    html = polite_get(feed_url)
    return parse_listings(html, source=source, manager=manager, feed_url=feed_url)
