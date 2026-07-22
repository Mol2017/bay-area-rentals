"""Tests for the field parsers and the listing classifier.

These are the correctness-critical part of the project: every source words the
same four facts differently, and ``base.py`` is what turns that variety into
one schema. Most cases below are real strings taken from the live feeds, so a
regression here means the dashboard starts publishing wrong rents, wrong
bedroom counts, or storage lockers presented as apartments.

Run with::

    python3 -m pytest tests/ -q      # or: python3 tests/test_parsers.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

import pytest  # noqa: E402

from base import (  # noqa: E402
    KIND_APPLICATION,
    KIND_COMMERCIAL,
    KIND_PARKING,
    KIND_RESIDENTIAL,
    KIND_ROOM,
    KIND_STORAGE,
    classify_listing,
    clean_text,
    infer_bedrooms_from_title,
    normalize_city,
    parse_available,
    parse_bathrooms,
    parse_bedrooms,
    parse_location,
    parse_rent,
    parse_square_feet,
)


# ── rent ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("$3,295", 3295.0),                 # AppFolio blurb
    ("$1,200", 1200.0),                 # Buildium rendered price
    ("1200.00", 1200.0),                # Buildium data-rent attribute
    ("810.0", 810.0),                   # Tripalink minPrice string
    ("$2,026.15", 2026.15),
    ("  $ 4,750  ", 4750.0),
    # A range advertises the low end as the entry price; that is the number a
    # renter filters on.
    ("$2,000 - $3,000", 2000.0),
    ("$2,700 – 2 Bed-1Bath", 2700.0),   # Boston PM combined line
    ("Call for pricing", None),
    ("", None),
    (None, None),
    ("$50", None),                      # below MIN_RENT -- a fee, not rent
    ("$1,200,000", None),               # a sale price leaking in
])
def test_parse_rent(text, expected):
    assert parse_rent(text) == expected


# ── bedrooms ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("2 bd / 1 ba", 2.0),               # AppFolio
    ("2 Bed | 1 Bath | 729 sqft", 2.0),  # Buildium features line
    ("3 bedrooms", 3.0),
    ("2", 2.0),                         # Buildium data-bedrooms attribute
    ("3.0", 3.0),                       # Tripalink floorPlanBedroomNum
    ("Studio", 0.0),
    ("studio / 1 ba", 0.0),             # studio wins over the numeric pattern
    ("Efficiency", 0.0),
    ("1.5 bd", 1.5),                    # ReListo publishes half-bedrooms
    ("$2,700 – 2 Bed-1Bath", 2.0),
    ("", None),
    (None, None),
    ("99 bd", None),                    # beyond MAX_BEDROOMS -- parser bug
])
def test_parse_bedrooms(text, expected):
    assert parse_bedrooms(text) == expected


def test_parse_bedrooms_studio_is_zero_not_none():
    """A studio must be 0.0, never None -- 0 is falsy and easy to lose."""
    result = parse_bedrooms("Studio")
    assert result == 0.0
    assert result is not None


@pytest.mark.parametrize("text,expected", [
    ("2 bd / 1.5 ba", 1.5),
    ("2 Bed | 1 Bath", 1.0),
    ("1", 1.0),
    ("", None),
])
def test_parse_bathrooms(text, expected):
    assert parse_bathrooms(text) == expected


# ── availability ──────────────────────────────────────────────────────────

TODAY = date(2026, 7, 22)


@pytest.mark.parametrize("text,expected_date,expected_now", [
    ("8/2/26", "2026-08-02", False),            # AppFolio short date
    ("08/02/2026", "2026-08-02", False),
    ("2026-08-02", "2026-08-02", False),        # ISO
    ("Available 8/2/26", "2026-08-02", False),
    ("available September 1", "2026-09-01", False),   # Buildium, no year
    ("Available Aug 1st", "2026-08-01", False),       # ShowMojo ordinal
    ("Sept 1, 2026", "2026-09-01", False),
    ("NOW", None, True),
    ("Available Now", None, True),
    ("Availability: Now", None, True),
    ("", None, False),
    (None, None, False),
    ("Call for availability", None, False),
])
def test_parse_available(text, expected_date, expected_now):
    iso, now = parse_available(text, today=TODAY)
    assert iso == expected_date
    assert now == expected_now


def test_parse_available_prefers_date_over_now_keyword():
    """'Available 8/2/26' contains 'available' but is not available now."""
    iso, now = parse_available("Available 8/2/26", today=TODAY)
    assert iso == "2026-08-02"
    assert now is False


def test_yearless_date_rolls_forward_when_well_past():
    """A year-less date far in the past means next year's date."""
    iso, _ = parse_available("available January 5", today=TODAY)
    assert iso == "2027-01-05"


def test_yearless_date_stays_in_grace_window():
    """A recently-passed date means the unit is already open, not next year."""
    iso, _ = parse_available("available July 1", today=TODAY)
    assert iso == "2026-07-01"


# ── location ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,city,state,postal", [
    ("2505 Virginia St #10, Berkeley, CA 94709", "Berkeley", "CA", "94709"),
    ("399 Schafer Rd,, #18, Hayward, CA 94544", "Hayward", "CA", "94544"),
    ("Fresno,CA|93726", "Fresno", "CA", "93726"),      # Buildium attribute
    ("Berkeley, CA 94709", "Berkeley", "CA", "94709"),
    ("1919 E. Sussex Way, 112, Fresno, CA 93726", "Fresno", "CA", "93726"),
    ("no city here", None, None, None),
    ("", None, None, None),
    (None, None, None, None),
])
def test_parse_location(text, city, state, postal):
    assert parse_location(text) == (city, state, postal)


@pytest.mark.parametrize("raw,expected", [
    ("san francisco", "San Francisco"),
    ("San francisco", "San Francisco"),   # source casing drift
    ("SAN FRANCISCO", "San Francisco"),
    ("  Berkeley  ", "Berkeley"),
    ("314 Perkins St.", None),            # a street that leaked into the slot
    ("94103", None),                      # a ZIP that leaked into the slot
    ("", None),
    (None, None),
])
def test_normalize_city(raw, expected):
    assert normalize_city(raw) == expected


def test_city_casing_collapses_to_one_facet():
    """Two spellings of one city must not split the dashboard's city filter."""
    assert normalize_city("San francisco") == normalize_city("SAN FRANCISCO")


# ── square feet ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("750", 750),
    ("2 Bed | 1 Bath | 729 sqft", 729),
    ("1,271 sq ft", 1271),
    ("", None),
    (None, None),
])
def test_parse_square_feet(text, expected):
    assert parse_square_feet(text) == expected


# ── classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("address,title,beds,expected", [
    # Real apartments stay residential.
    ("2505 Virginia St #10, Berkeley, CA", "Top Floor Spacious Unit", 2.0,
     KIND_RESIDENTIAL),
    ("1919 E. Sussex Way, Fresno, CA", None, 2.0, KIND_RESIDENTIAL),

    # An apartment that merely *mentions* parking or storage is still an
    # apartment -- this is the main false-positive risk in the classifier.
    ("123 Main St", "Stunning 1-bedroom with Pool/Parking/Laundry", 1.0,
     KIND_RESIDENTIAL),
    ("123 Main St", "Large 1BR W/ Great Storage & Park Views!", 1.0,
     KIND_RESIDENTIAL),
    ("123 Main St", "GORGEOUS Design! HUGE Bedroom + Parking!", 1.0,
     KIND_RESIDENTIAL),

    # Non-units.
    ("19 Croxton Ave., Oakland, CA", "Drive-up Storage Garage w/ 24/7 Access",
     None, KIND_STORAGE),
    ("2828 College Avenue - Storage Unit - Not an Apartment", "Storage!", None,
     KIND_STORAGE),
    ("2091 California Street - Non-Res Rental5", "Parking Space Near UC!", None,
     KIND_PARKING),
    ("2709 Dwight Way , Parking, Berkeley, CA", None, None, KIND_PARKING),
    ("644 40th Street, Oakland, CA", "private small office in Oakland", None,
     KIND_COMMERCIAL),
    ("1604 Vallejo St, SF", "Prime Commercial Retail or Office Space", None,
     KIND_COMMERCIAL),

    # Application placeholders -- these carry bedroom counts, so they must be
    # caught before the bedroom gate.
    ("AP Management Rental Application", "Rental Application", None,
     KIND_APPLICATION),
    ("2880 Sacramento St", "NOT AVAILABLE - FOR ROOMMATE ADD-ON ONLY - NOT ON MARKET",
     2.0, KIND_APPLICATION),
    ("123 Elm St", "POSTED ONLY FOR ROOMMATE SWITCH - (NOT ON MARKET)", 3.0,
     KIND_APPLICATION),

    # Per-room rentals.
    ("858 Washington St # 40, SF", "Private SRO Room in Chinatown", 0.0,
     KIND_ROOM),
    ("2338 Telegraph Ave. - Standard Room", "Standard Room", None, KIND_ROOM),
    ("2099 MLK Jr Way", "By the Bed: Rent by Bed at RUMI", None, KIND_ROOM),
    ("2077 Haste St", "#611 Room 1 Bed B Luxury Shared Furnished Suite", 0.0,
     KIND_ROOM),
    ("100 Foo St", "Shared Bedroom in 2-Bedroom Apartment", 2.0, KIND_ROOM),
])
def test_classify_listing(address, title, beds, expected):
    assert classify_listing(address, title, beds) == expected


def test_bedroom_gate_protects_real_apartments():
    """The parking/storage patterns must never fire on a unit with bedrooms."""
    for title in ["Apartment with Parking", "Unit with Storage", "Near the office"]:
        assert classify_listing("1 Main St", title, 2.0) == KIND_RESIDENTIAL


# ── title-based bedroom recovery ──────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Top-Floor Studio Near UC Berkeley North Gate", 0.0),
    ("Spacious 3 Bedroom Flat", 3.0),
    ("Charming home near campus", None),
    ("", None),
    (None, None),
])
def test_infer_bedrooms_from_title(title, expected):
    assert infer_bedrooms_from_title(title) == expected


# ── html helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("html,expected", [
    ("<div> Hello   world </div>", "Hello world"),
    ("2 bd &amp; 1 ba", "2 bd & 1 ba"),
    ("10 Merrymount # 403 &#8211; Quincy", "10 Merrymount # 403 – Quincy"),
    ("&nbsp;&bull;&nbsp;", "•"),
    ("", ""),
    (None, ""),
])
def test_clean_text(html, expected):
    assert clean_text(html) == expected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
