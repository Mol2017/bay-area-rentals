"""Helpers shared across scrapers: HTTP fetching and field parsing.

The canonical data shape lives in ``scrapers/schema.py`` and is re-exported
here so an individual scraper can import everything it needs from one place.

What lives here:

  * ``polite_get`` -- an HTTP GET that throttles to at most one request per
    ``REQUEST_DELAY_SECONDS`` per host, retries on transient failures, and
    sends a real browser User-Agent (several of these portals 403 the Python
    default UA).
  * Field parsers -- ``parse_rent``, ``parse_bedrooms``, ``parse_available``,
    ``city_from_address``. These are the correctness-critical part of the
    project: every source words the same four facts differently, and these
    functions are what turn that variety into one schema. They are covered by
    ``tests/test_parsers.py``.
"""
from __future__ import annotations

import html as html_module
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

# Re-export schema types so scrapers can do `from base import ...`.
from schema import (  # noqa: F401  (re-export)
    MAX_BEDROOMS,
    MAX_RENT,
    MIN_RENT,
    PACIFIC,
    RAW_DIR,
    REPO_ROOT,
    SCHEMA_VERSION,
    SchemaError,
    ScrapeResult,
    Unit,
    dedupe_units,
    write_result,
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Minimum seconds between two requests to the *same host*. Different hosts
# proceed in parallel; we only want to avoid hammering any single portal.
REQUEST_DELAY_SECONDS = 1.0

_host_lock = threading.Lock()
_last_request_at: dict[str, float] = {}


def _throttle(host: str) -> None:
    with _host_lock:
        elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)
        _last_request_at[host] = time.monotonic()


def polite_get(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> str:
    """GET *url* and return the decoded body.

    Throttles per-host, retries transient errors with exponential backoff,
    and raises the last exception if every attempt fails. 404 and 403 are
    treated as permanent and raise immediately -- retrying them just wastes
    the daily run's time budget.
    """
    host = urllib.parse.urlsplit(url).netloc
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)

    last_exc: Exception | None = None
    for attempt in range(retries):
        _throttle(host)
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                raise
            last_exc = e
        except Exception as e:  # noqa: BLE001  (network errors are varied)
            last_exc = e
        if attempt < retries - 1:
            time.sleep(2**attempt)

    assert last_exc is not None
    raise last_exc


# ── HTML utilities ────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(markup: str | None) -> str:
    """Strip tags, decode HTML entities, and collapse whitespace.

    Entity decoding goes through ``html.unescape`` rather than a hand-kept
    table: these feeds use a long tail of entities (``&bull;`` as a field
    separator on ERI, ``&#8211;`` before the city on Boston PM, assorted
    accented characters in listing copy) and any table we maintained by hand
    would keep coming up one entity short.

    ``&nbsp;`` decodes to U+00A0, which the final whitespace collapse
    normalizes to a plain space -- so downstream regexes never have to
    special-case it.
    """
    if not markup:
        return ""
    text = _TAG_RE.sub(" ", markup)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ── field parsers ─────────────────────────────────────────────────────────

_RENT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")


def parse_rent(text: str | None) -> float | None:
    """Extract a monthly rent from free-form text.

    Handles ``$3,295``, ``$3295.00``, ``3295`` and ranges such as
    ``$2,000 - $3,000`` (the low end is returned, since that is the figure a
    renter filters on). Returns None for "Call for pricing", empty input, or
    any value outside the sanity bounds in ``schema``.
    """
    if not text:
        return None
    s = clean_text(text) if "<" in str(text) else str(text).strip()

    matches = _RENT_RE.findall(s)
    if not matches:
        # Buildium exposes a bare numeric attribute (data-rent="1200.00").
        bare = re.fullmatch(r"\s*([\d,]+(?:\.\d{1,2})?)\s*", s)
        if not bare:
            return None
        matches = [bare.group(1)]

    values: list[float] = []
    for raw in matches:
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    values = [v for v in values if MIN_RENT <= v <= MAX_RENT]
    if not values:
        return None
    return min(values)


_STUDIO_RE = re.compile(r"\b(studio|efficiency|bachelor)\b", re.I)
_BED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bd|bed|bedroom|br|b/r)s?\b", re.I)


def parse_bedrooms(text: str | None) -> float | None:
    """Extract a bedroom count. Studio maps to 0.

    Handles ``2 bd / 1 ba``, ``2 Bed | 1 Bath``, ``3 bedrooms``, ``Studio``,
    and a bare ``2`` (Buildium's ``data-bedrooms`` attribute). Checks for
    "studio" *before* the numeric pattern so "Studio / 1 ba" isn't misread.
    """
    if text is None:
        return None
    s = clean_text(text) if "<" in str(text) else str(text).strip()
    if not s:
        return None

    if _STUDIO_RE.search(s):
        return 0.0

    m = _BED_RE.search(s)
    if m:
        try:
            beds = float(m.group(1))
        except ValueError:
            return None
        return beds if 0 <= beds <= MAX_BEDROOMS else None

    bare = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", s)
    if bare:
        try:
            beds = float(bare.group(1))
        except ValueError:
            return None
        return beds if 0 <= beds <= MAX_BEDROOMS else None

    return None


_BATH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ba|bath|bathroom)s?\b", re.I)


def parse_bathrooms(text: str | None) -> float | None:
    if text is None:
        return None
    s = clean_text(text) if "<" in str(text) else str(text).strip()
    m = _BATH_RE.search(s)
    if m:
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        return v if 0 <= v <= MAX_BEDROOMS else None
    bare = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", s)
    if bare:
        try:
            v = float(bare.group(1))
        except ValueError:
            return None
        return v if 0 <= v <= MAX_BEDROOMS else None
    return None


_SQFT_RE = re.compile(r"([\d,]{2,7})\s*(?:sq\.?\s*(?:ft|feet)|sqft|square\s*feet)", re.I)


def parse_square_feet(text: str | None) -> int | None:
    if not text:
        return None
    s = clean_text(text) if "<" in str(text) else str(text).strip()
    m = _SQFT_RE.search(s)
    raw = m.group(1) if m else (s if re.fullmatch(r"[\d,]{2,7}", s) else None)
    if not raw:
        return None
    try:
        v = int(raw.replace(",", ""))
    except ValueError:
        return None
    return v if 0 < v < 100_000 else None


_NOW_RE = re.compile(r"\b(now|immediate(?:ly)?|today|available\s+now|vacant)\b", re.I)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
    re.I,
)
_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# How far into the past a year-less date may fall before we roll it forward.
# A listing saying "available July 1" on July 22 means it is already open,
# not that it opens next July -- so we allow a grace window before assuming
# the source meant next year.
_YEARLESS_GRACE_DAYS = 60


def _infer_year(month: int, day: int, today: date) -> int:
    """Choose the year for a date given without one.

    Prefers the current year, rolling to next year only once the date is
    more than ``_YEARLESS_GRACE_DAYS`` in the past.
    """
    try:
        candidate = date(today.year, month, day)
    except ValueError:  # e.g. Feb 29 in a non-leap year
        return today.year
    if (today - candidate).days > _YEARLESS_GRACE_DAYS:
        return today.year + 1
    return today.year


def parse_available(text: str | None, *, today: date | None = None) -> tuple[str | None, bool]:
    """Parse an availability string into ``(iso_date_or_None, available_now)``.

    Returns a 2-tuple because "Now" and a concrete date are different claims
    and the frontend renders them differently -- see ``Unit.available_now``.

    Handles ``Now`` / ``Available Now``, ``8/2/26``, ``08/02/2026``,
    ``available September 1``, ``Sept 1, 2026`` and ISO ``2026-08-02``.
    Two-digit years are windowed to 2000-2099. Returns ``(None, False)`` when
    nothing parses, which is a legitimate outcome the schema allows.
    """
    if not text:
        return None, False
    today = today or datetime.now(PACIFIC).date()
    s = clean_text(text) if "<" in str(text) else str(text).strip()
    if not s:
        return None, False

    # ISO first -- unambiguous, so it should win over the numeric pattern.
    m = _ISO_DATE_RE.search(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat(), False
        except ValueError:
            pass

    m = _MONTH_NAME_RE.search(s)
    if m:
        token = m.group(1).lower().rstrip(".")
        # Try the full token first so "sept" hits its own key, then fall back
        # to the 3-letter abbreviation that covers every other month.
        mon = _MONTHS.get(token) or _MONTHS.get(token[:3])
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else _infer_year(mon or 1, day, today)
        if mon:
            try:
                return date(year, mon, day).isoformat(), False
            except ValueError:
                pass

    m = _NUMERIC_DATE_RE.search(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        raw_y = m.group(3)
        if raw_y is None:
            year = _infer_year(mo, d, today)
        else:
            year = int(raw_y)
            if year < 100:  # two-digit year -> 2000s
                year += 2000
        try:
            return date(year, mo, d).isoformat(), False
        except ValueError:
            pass

    # Only treat it as "now" if no date parsed -- "Available Now" wins, but
    # "Available 8/2/26" should not be flagged as immediately available just
    # because it contains the word "available".
    if _NOW_RE.search(s):
        return None, True

    return None, False


# City/state/ZIP tail of a US address: ", Berkeley, CA 94709"
_CITY_STATE_ZIP_RE = re.compile(
    r",\s*([A-Za-z][A-Za-z\.\-' ]+?)\s*,\s*([A-Z]{2})\b(?:\s*(\d{5})(?:-\d{4})?)?\s*$"
)
# Buildium's data-location attribute: "Fresno,CA|93726"
_PIPE_LOCATION_RE = re.compile(r"^\s*([^,|]+?)\s*,\s*([A-Za-z]{2})\s*(?:\|\s*(\d{5}))?\s*$")


# A "city" containing digits, or ending in a street type, is a mis-parse --
# a street line that landed in the city slot. Better to report no city than a
# wrong one, since city is the dashboard's primary filter.
_STREET_SUFFIX_RE = re.compile(
    r"\b(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|ct|court"
    r"|pl|place|way|ter|terrace|pkwy|parkway|cir|circle|hwy|highway)\.?\s*$",
    re.I,
)


def normalize_city(city: str | None) -> str | None:
    """Canonicalize a city name, or return None if it isn't plausibly one.

    Sources disagree on casing for the same place ("San Francisco" vs "San
    francisco"), which would otherwise split one city into two facets, so
    everything is normalized to title case. ``Mc``/``Mac``/``O'`` prefixes and
    interior capitals are preserved.
    """
    if not city:
        return None
    city = re.sub(r"\s+", " ", str(city)).strip(" ,.-")
    if not city:
        return None
    if any(ch.isdigit() for ch in city):
        return None
    if _STREET_SUFFIX_RE.search(city):
        return None

    def _word(w: str) -> str:
        if re.match(r"^(mc|mac)[a-z]", w, re.I) and len(w) > 3:
            return w[:2].capitalize() + w[2:].capitalize() if w[:2].lower() == "mc" \
                else w[:3].capitalize() + w[3:].capitalize()
        if "'" in w:
            head, _, tail = w.partition("'")
            return f"{head.capitalize()}'{tail.capitalize()}"
        if "-" in w:
            return "-".join(p.capitalize() for p in w.split("-"))
        return w.capitalize()

    return " ".join(_word(w) for w in city.split())


def parse_location(text: str | None) -> tuple[str | None, str | None, str | None]:
    """Parse ``(city, state, postal_code)`` out of an address-ish string.

    Accepts a full street address (``2505 Virginia St #10, Berkeley, CA
    94709``), a bare city/state line (``Berkeley, CA 94709``), and Buildium's
    pipe-delimited attribute (``Fresno,CA|93726``). Returns ``(None, None,
    None)`` when no city is confidently identifiable -- guessing a city from
    an unrecognized format would silently corrupt the dashboard's main filter.
    """
    if not text:
        return None, None, None
    s = clean_text(text) if "<" in str(text) else str(text).strip()
    if not s:
        return None, None, None

    m = _PIPE_LOCATION_RE.match(s)
    if m:
        return normalize_city(m.group(1)), m.group(2).upper(), m.group(3)

    m = _CITY_STATE_ZIP_RE.search(s)
    if m:
        return normalize_city(m.group(1)), m.group(2).upper(), m.group(3)

    # "Berkeley, CA 94709" with no leading street component.
    m = re.fullmatch(
        r"\s*([A-Za-z][A-Za-z\.\-' ]+?)\s*,\s*([A-Z]{2})\b(?:\s*(\d{5})(?:-\d{4})?)?\s*", s
    )
    if m:
        return normalize_city(m.group(1)), m.group(2).upper(), m.group(3)

    return None, None, None


def city_from_address(address: str | None) -> str | None:
    """Convenience wrapper returning just the city."""
    return parse_location(address)[0]


# ── listing classification ────────────────────────────────────────────────
#
# Portals list more than apartments. AppFolio in particular carries "Rental
# Application" pseudo-listings (an application form dressed up as a unit),
# and several managers advertise parking stalls, storage lockers and
# commercial suites through the same feed. None of those are units a renter
# can move into, so they must not inflate the dashboard's counts or drag its
# rent range down to $125/mo.
#
# The patterns below match against the address and title ONLY -- never the
# free-text description, where "assigned parking" and "extra storage" are
# common amenity phrases on perfectly ordinary apartments.

KIND_RESIDENTIAL = "residential"  # a whole apartment / house
KIND_ROOM = "room"  # a private room inside a shared unit
KIND_APPLICATION = "application"
KIND_PARKING = "parking"
KIND_STORAGE = "storage"
KIND_COMMERCIAL = "commercial"

# Kinds a renter can actually live in. The others are excluded from the
# dashboard by scripts/merge.py.
LIVABLE_KINDS = {KIND_RESIDENTIAL, KIND_ROOM}

# Co-living and SRO operators list a single room inside a shared N-bedroom
# apartment. Those are real housing and belong in the dataset, but they must
# not be counted as whole N-bedroom units -- an $810 "3 bedroom" is one room
# in a 3-bedroom flat, and letting it through as a 3BR would make the
# bedroom filter useless for anyone looking for a family-sized apartment.
_ROOM_RE = re.compile(
    r"\bSRO\b"
    r"|\b(private|shared|standard|select|deluxe|premium)\s+(room|bedroom)\b"
    r"|\broom\s+in\s+(a|an|the)\b"
    r"|\bshared\s+(apartment|house|unit|flat|suite)\b"
    r"|\bco-?living\b|\broommate\s+matching\b"
    r"|\bby\s+the\s+bed\b|\bBTB\b"
    # "#611 Room 1 Bed B" -- a bed within a numbered room.
    r"|\broom\s+\d+\s*bed\b",
    re.I,
)

# Unambiguous: these are never rentable units regardless of any other field.
# Managers post application forms and "roommate switch only" placeholders
# through the same feed as real vacancies, and those DO carry bedroom counts
# -- so they have to be caught before the bedroom gate below.
_ALWAYS_EXCLUDE = [
    (KIND_APPLICATION, re.compile(
        r"rental\s+application|roommate\s+(change|switch|add-?on)"
        r"|sublet\s+application|application\s+only|waitlist\s+application"
        r"|co-?signer\s+add-?on|\bnot\s+on\s+(the\s+)?market\b", re.I)),
    (KIND_STORAGE, re.compile(r"not\s+an?\s+apartment", re.I)),
]

# Ambiguous on their own -- "Sunny 2BR with Parking" is an apartment, and 28
# listings in the current dataset mention parking in exactly that way. These
# patterns therefore only classify when the listing has no real bedroom
# count, which is what separates a parking stall from a flat that includes
# one.
_EXCLUDE_IF_NO_BEDROOMS = [
    (KIND_PARKING, re.compile(
        r"\bparking\b|\bnon-?res(idential)?\b|\bcarport\b"
        r"|\bgarage\s+(space|spot|stall)\b", re.I)),
    (KIND_STORAGE, re.compile(r"\bstorage\b|\blocker\b", re.I)),
    (KIND_COMMERCIAL, re.compile(
        r"\bcommercial\b|\bretail\b|\boffice\b|\bwarehouse\b|\bfor\s+lease\b"
        r"|\bmedical\s+(use|suite)|\bsalon\b", re.I)),
]


def classify_listing(
    address: str | None, title: str | None, bedrooms: float | None
) -> str:
    """Classify a listing as residential or as one of the excluded kinds.

    ``bedrooms`` is consulted so an apartment advertising "with Parking" in
    its title is not mistaken for a parking stall: a listing with 1+ bedrooms
    is residential no matter what its title says.
    """
    haystack = f"{address or ''} | {title or ''}"

    for kind, pattern in _ALWAYS_EXCLUDE:
        if pattern.search(haystack):
            return kind

    if bedrooms is None or bedrooms == 0:
        for kind, pattern in _EXCLUDE_IF_NO_BEDROOMS:
            if pattern.search(haystack):
                return kind

    if _ROOM_RE.search(haystack):
        return KIND_ROOM

    return KIND_RESIDENTIAL


_TITLE_STUDIO_RE = re.compile(r"\bstudio\b", re.I)


def infer_bedrooms_from_title(title: str | None) -> float | None:
    """Last-resort bedroom recovery from a listing title.

    Some listings omit the bed/bath element entirely but say "Top-Floor
    Studio Near UC Berkeley" in the title. Only used when the structured
    field is missing -- never to override it.
    """
    if not title:
        return None
    if _TITLE_STUDIO_RE.search(title):
        return 0.0
    m = _BED_RE.search(title)
    if m:
        try:
            beds = float(m.group(1))
        except ValueError:
            return None
        return beds if 0 <= beds <= MAX_BEDROOMS else None
    return None
