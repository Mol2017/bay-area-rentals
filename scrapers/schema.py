"""Unified data schema every scraper must conform to.

Each scraper produces a single JSON file in ``data/raw/<source>.json`` with
this top-level shape::

    {
      "schema_version": 1,
      "source":      str,            # snake_case slug, matches the filename
      "manager":     str,            # human-readable property manager name
      "source_url":  str,            # page the data was scraped from
      "scraped_at":  str (ISO-8601), # set by ScrapeResult.to_dict()
      "units":       [Unit, ...]     # 0 or more
    }

A ``Unit`` is one available rental listing. The four fields the dashboard is
built around are ``city``, ``bedrooms``, ``available_date`` and ``rent`` --
everything else is context that makes a listing actionable (address, link,
photo, square footage).

Every one of those four is **optional at the type level** because real
listings genuinely omit them: a listing may say "Call for pricing", or give
no availability date. Emitting the unit with ``rent=None`` is more honest
than dropping it or inventing a number, so the frontend is written to
tolerate nulls in all four. ``Unit.validate()`` therefore only enforces what
must always be true -- an address to identify the unit, and correct types /
ranges on anything that *is* present.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

SCHEMA_VERSION = 1

# Sanity bounds. These exist to catch parser bugs (a mis-parsed "$1,200,000"
# sale price leaking into a rent field, a bedroom count read off a square
# footage element), not to filter out legitimately unusual listings -- so
# they are deliberately wide.
MIN_RENT = 100.0
MAX_RENT = 100_000.0
MAX_BEDROOMS = 20.0

# ── Controlled vocabularies ───────────────────────────────────────────────
# Every one of these is nullable. None means "the source didn't say", which
# is a different and more honest claim than the negative value: `parking:
# None` means unknown, `parking: "none"` means the listing states there is no
# parking. The UI must not render the two the same way.

UNIT_TYPE_STUDIO = "studio"
UNIT_TYPES = {UNIT_TYPE_STUDIO, "1_bedroom", "2_bedroom", "3plus_bedroom"}

# What you actually rent, as distinct from the floor plan it sits in. A
# private room in a 3-bedroom flat is (private_room, 3plus_bedroom).
#
# Nullable, and the null carries meaning. Read together with ``kind`` there
# are four states, not three:
#
#   room_type=entire_place   a whole unit
#   room_type=private_room   your own bedroom in a shared unit
#   room_type=shared_room    you share the bedroom itself
#   room_type=None           not yet determined
#
# **Adapters must never write `entire_place`.** It reads as a determination
# but was only ever the schema default, and a non-null default silently
# outranks a later, better answer: when `classify_listing` missed a room, the
# defaulted `entire_place` blocked the enrichment pass from correcting it and
# 113 units -- 110 of them per-bed listings priced $950-$1,200 -- were
# published as whole apartments. The default is now applied last, in
# merge.py, only to units nothing else could resolve.
#
# The None state is also load-bearing in its own right: an operator running
# roommate matching with a 2-tenant cap has not said whether the second
# tenant is your partner or a stranger, and no parsing recovers it.
ROOM_TYPE_ENTIRE = "entire_place"
ROOM_TYPES = {ROOM_TYPE_ENTIRE, "private_room", "shared_room"}

PETS_VALUES = {"allowed", "cats_only", "dogs_only", "none"}
FURNISHED_VALUES = {"furnished", "partial", "unfurnished"}
PARKING_VALUES = {"garage", "off_street", "street", "none"}
LAUNDRY_VALUES = {"in_unit", "shared", "hookups", "none"}

# Fields the enrichment pass may write onto a unit. `unit_type` is absent on
# purpose -- it is derived from `bedrooms` and must never be set externally.
ENRICHED_ASSIGNABLE = ("room_type", "pets", "furnished", "parking", "laundry")

_ENUM_FIELDS = {
    "unit_type": UNIT_TYPES,
    "room_type": ROOM_TYPES,
    "pets": PETS_VALUES,
    "furnished": FURNISHED_VALUES,
    "parking": PARKING_VALUES,
    "laundry": LAUNDRY_VALUES,
}


def derive_unit_type(bedrooms: float | None) -> str | None:
    """Bucket a bedroom count into the coarse ``unit_type`` label.

    Half-bedrooms floor into their integer bucket -- a "1.5 bd" is a
    one-bedroom-plus-den, and giving it its own bucket produced a
    single-listing column in the rent chart whose median read as a real
    market rate. Returns None for an unknown bedroom count rather than
    guessing; `studio` is a claim the listing has to actually make.
    """
    if bedrooms is None:
        return None
    if bedrooms < 1:
        # 0 is a studio; so is a sub-one "0.5 bd" junior -- flooring it to
        # int gave the bogus label "0_bedroom", which is not even a valid
        # unit_type, and slipped through because reconcile.py writes raw
        # dicts without re-validating.
        return UNIT_TYPE_STUDIO
    n = int(bedrooms)  # floors 1.5 -> 1, 2.5 -> 2
    if n >= 3:
        return "3plus_bedroom"
    return f"{n}_bedroom"


class SchemaError(ValueError):
    """Raised when a Unit or ScrapeResult fails validation."""


@dataclass
class Unit:
    """One available rental unit.

    ``bedrooms`` uses 0 to mean studio, and is a float because listings do
    sometimes advertise "1.5 bd" for a den/junior configuration.

    ``available_date`` is an ISO ``YYYY-MM-DD`` date string, or None when the
    source doesn't say. ``available_now`` is tracked separately because "Now"
    and "available on <today's date>" are different claims -- a unit listed as
    immediately available stays immediately available tomorrow, whereas a
    date silently rots. Frontends should prefer ``available_now`` when set.
    """

    # Identity
    address: str
    source: str
    manager: str

    # The four headline fields
    city: str | None = None
    bedrooms: float | None = None
    available_date: str | None = None  # ISO YYYY-MM-DD
    rent: float | None = None  # USD / month

    # Classification. Portals list parking stalls, storage lockers,
    # commercial suites and "Rental Application" pseudo-listings through the
    # same feed as apartments. We keep them in the raw files for traceability
    # but tag them here so the merge step can exclude them -- see
    # base.classify_listing.
    kind: str = "residential"

    # What you rent, vs. the floor plan it sits in. ``unit_type`` is derived
    # from ``bedrooms`` in __post_init__ so no adapter has to set it and the
    # two can never disagree. ``room_type`` stays None until something
    # establishes it -- see the vocabulary note above.
    unit_type: str | None = None
    room_type: str | None = None

    # Amenities. Stated inconsistently by every source ("Fully-Furnished
    # By-The-Bed Unit", "On-site laundry facilities", "Cats allowed"), so
    # these are populated by scrapers/enrich.py rather than by the adapters.
    pets: str | None = None
    furnished: str | None = None
    parking: str | None = None
    laundry: str | None = None

    # Human-readable blurb generated from the listing's own detail page.
    # Distinct from ``notes``, which carries machine-generated provenance
    # ("rent shown is per room") that must survive independently.
    summary: str | None = None

    # Context
    available_now: bool = False
    bathrooms: float | None = None
    square_feet: int | None = None
    state: str | None = "CA"
    postal_code: str | None = None
    title: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        # Derived, never supplied. Recomputed on every construction so a
        # bedroom count corrected by a later parse can't leave a stale label.
        self.unit_type = derive_unit_type(self.bedrooms)

        # Defaulted here rather than on the field, so that every adapter gets
        # it right without knowing about it. A whole unit is `entire_place`;
        # a room whose adapter said nothing stays None, meaning "this is a
        # room and nobody has established whether the bedroom is shared".
        # Defaulting rooms to `entire_place` would publish a per-room price
        # under a whole-apartment label -- the exact failure `kind` exists to
        # prevent. Adapters that do know (AppFolio's "Shared Room in 1 bd",
        # Tripalink's 1-tenant cap) pass a value and it is kept.
        # Deliberately NOT defaulting room_type here. Applying it at
        # construction makes a schema default indistinguishable from a
        # determination, and a non-null default silently outranks the
        # enrichment pass that would have corrected it. merge.py applies it
        # last, once nothing better is available.

    def validate(self) -> None:
        if not self.address or not self.address.strip():
            raise SchemaError(f"[{self.source}] Unit is missing an address")
        if not self.source:
            raise SchemaError(f"Unit at {self.address!r} is missing a source")

        if self.rent is not None:
            if not isinstance(self.rent, (int, float)):
                raise SchemaError(f"[{self.source}] {self.address!r} rent must be numeric")
            if not (MIN_RENT <= self.rent <= MAX_RENT):
                raise SchemaError(
                    f"[{self.source}] {self.address!r} rent {self.rent} outside "
                    f"[{MIN_RENT}, {MAX_RENT}] -- likely a parser bug"
                )

        if self.bedrooms is not None:
            if not isinstance(self.bedrooms, (int, float)):
                raise SchemaError(f"[{self.source}] {self.address!r} bedrooms must be numeric")
            if not (0 <= self.bedrooms <= MAX_BEDROOMS):
                raise SchemaError(
                    f"[{self.source}] {self.address!r} bedrooms {self.bedrooms} "
                    f"outside [0, {MAX_BEDROOMS}] -- likely a parser bug"
                )

        if self.available_date is not None:
            try:
                datetime.strptime(self.available_date, "%Y-%m-%d")
            except (ValueError, TypeError):
                raise SchemaError(
                    f"[{self.source}] {self.address!r} available_date "
                    f"{self.available_date!r} is not ISO YYYY-MM-DD"
                )

        if self.square_feet is not None and self.square_feet <= 0:
            raise SchemaError(f"[{self.source}] {self.address!r} square_feet must be positive")

        # Controlled vocabularies. A typo'd enum ("in-unit" for "in_unit")
        # would silently disappear from every facet count rather than error,
        # so it is worth failing the unit outright.
        for name, allowed in _ENUM_FIELDS.items():
            value = getattr(self, name)
            if value is not None and value not in allowed:
                raise SchemaError(
                    f"[{self.source}] {self.address!r} {name}={value!r} is not one of "
                    f"{sorted(allowed)}"
                )

        # room_type may be None -- see the vocabulary note above. What is not
        # allowed is claiming a whole unit while `kind` says it is a room,
        # which would hide a per-room price behind an apartment label.
        if self.kind == "room" and self.room_type == ROOM_TYPE_ENTIRE:
            raise SchemaError(
                f"[{self.source}] {self.address!r} is classified as a room but "
                f"room_type={ROOM_TYPE_ENTIRE!r}; use None when sharing is undetermined"
            )

    @property
    def dedupe_key(self) -> tuple:
        """Identity used to collapse the same unit listed twice.

        Address alone is not enough: a building lists "1919 Sussex Way" once
        per available unit, and those are genuinely different units that
        differ by bedroom count or rent. Including both keeps them distinct
        while still collapsing true duplicates (the same unit appearing on
        both a manager's own site and its portal subdomain).
        """
        return (
            self.source,
            _normalize_address(self.address),
            self.bedrooms,
            self.rent,
        )


_ADDR_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Street-type abbreviations, so "2505 Virginia St" and "2505 Virginia Street"
# dedupe to the same key.
_STREET_ABBREV = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
    "road": "rd", "court": "ct", "lane": "ln", "place": "pl", "terrace": "ter",
    "parkway": "pkwy", "circle": "cir", "highway": "hwy", "square": "sq",
    "apartment": "apt", "suite": "ste", "unit": "",  "number": "",
    "north": "n", "south": "s", "east": "e", "west": "w",
}


def _normalize_address(addr: str) -> str:
    """Lowercase, strip punctuation, and canonicalize street abbreviations."""
    a = _ADDR_PUNCT_RE.sub(" ", (addr or "").lower())
    tokens = [_STREET_ABBREV.get(t, t) for t in _WHITESPACE_RE.split(a)]
    return " ".join(t for t in tokens if t)


@dataclass
class ScrapeResult:
    source: str
    manager: str
    source_url: str
    units: list[Unit] = field(default_factory=list)

    def add_unit(self, unit: Unit) -> None:
        unit.validate()
        self.units.append(unit)

    def validate(self) -> None:
        if not self.source or not self.source.replace("_", "").isalnum():
            raise SchemaError(
                f"ScrapeResult.source must be a snake_case slug, got {self.source!r}"
            )
        if not self.source_url:
            raise SchemaError(f"[{self.source}] ScrapeResult.source_url is required")
        for u in self.units:
            u.validate()

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "manager": self.manager,
            "source_url": self.source_url,
            "scraped_at": datetime.now(timezone.utc)
            .astimezone(PACIFIC)
            .isoformat(timespec="seconds"),
            "unit_count": len(self.units),
            "units": [asdict(u) for u in self.units],
        }


def dedupe_units(units: list[Unit]) -> list[Unit]:
    """Drop repeat listings, keeping the first occurrence.

    Portals routinely serve the same unit twice -- once under a paginated
    "all listings" view and once under a featured/filtered view. The original
    list is not mutated.
    """
    seen: set[tuple] = set()
    out: list[Unit] = []
    for u in units:
        key = u.dedupe_key
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def write_result(result: ScrapeResult) -> Path:
    """Validate, dedupe, sort, and write to ``data/raw/<source>.json``.

    Deduping happens here so every scraper benefits without having to
    remember to call it. Units are sorted by (city, bedrooms, rent) to keep
    git diffs between daily runs readable -- an unstable order would make
    every run look like a total rewrite.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    before = len(result.units)
    result.units = dedupe_units(result.units)
    if len(result.units) != before:
        print(f"  [{result.source}] deduped {before} -> {len(result.units)} units")

    result.units.sort(
        key=lambda u: (
            (u.city or "~").lower(),
            u.bedrooms if u.bedrooms is not None else 99,
            u.rent if u.rent is not None else 9e9,
            _normalize_address(u.address),
        )
    )

    out = RAW_DIR / f"{result.source}.json"
    out.write_text(json.dumps(result.to_dict(), indent=2))
    return out
