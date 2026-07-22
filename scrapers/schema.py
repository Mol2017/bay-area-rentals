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

    # Context
    available_now: bool = False
    bathrooms: float | None = None
    square_feet: int | None = None
    state: str | None = "CA"
    postal_code: str | None = None
    title: str | None = None
    url: str | None = None
    image: str | None = None
    notes: str | None = None

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
