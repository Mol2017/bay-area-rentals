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


# ── Enrichment: structured-field readers ─────────────────────────────────
#
# Only the free, structured tier is tested here. The prose-regex tier was
# deleted -- it produced every extraction bug found while building this, and
# saved nothing, since each detail page is fetched for its summary anyway.
# The knowledge it encoded now lives in the prompt in scrapers/llm.py.

import enrich as _enrich
from schema import derive_unit_type as _derive


def _policy(catalog, *entries):
    return [{"catalogName": catalog, "type": list(entries)}]


@pytest.mark.parametrize("bedrooms,expected", [
    (None, None), (0.0, "studio"), (1.0, "1_bedroom"), (1.5, "1_bedroom"),
    (2.0, "2_bedroom"), (2.5, "2_bedroom"), (3.0, "3plus_bedroom"),
    (7.0, "3plus_bedroom"),
])
def test_derive_unit_type(bedrooms, expected):
    assert _derive(bedrooms) == expected


def test_occupancy_cap_of_one_is_private():
    """A cap of 1 forecloses sharing -- nobody else can be placed in the room."""
    value, evidence = _enrich.room_type_from_occupancy(
        _policy("Occupancy Policy", "1 tenant max per room"), is_room=True
    )
    assert value == "private_room"
    assert evidence == "1 tenant max per room"


@pytest.mark.parametrize("entry", ["2 tenants max per room", "3 tenants max per room"])
def test_occupancy_cap_above_one_is_undetermined(entry):
    """A higher cap is ambiguous and must not be resolved from this field.

    It says how many people the room holds, not whether the second is the
    renter's partner (private) or a stranger the operator matched them with
    (shared). Both readings were implemented at some point and both were
    wrong; the model also answers this one confidently and wrongly. The only
    correct value from this field alone is "don't know".
    """
    value, _ = _enrich.room_type_from_occupancy(
        _policy("Occupancy Policy", entry), is_room=True
    )
    assert value is None


def test_occupancy_ignored_for_whole_units():
    """On a whole apartment the same policy is about headcount, not sharing."""
    value, _ = _enrich.room_type_from_occupancy(
        _policy("Occupancy Policy", "2 tenants max per room"), is_room=False
    )
    assert value is None


@pytest.mark.parametrize("entries,expected", [
    (("No Pets",), "none"),
    (("Cats Allowed", "Dogs Allowed"), "allowed"),
    (("Cats Allowed",), "cats_only"),
    (("Dogs Allowed",), "dogs_only"),
    ((), None),
])
def test_pets_from_policy(entries, expected):
    assert _enrich.pets_from_policy(_policy("Pet Policy", *entries))[0] == expected


def test_pets_policy_ignores_other_catalogs():
    assert _enrich.pets_from_policy(
        _policy("Application Policy", "Background Check")
    )[0] is None


@pytest.mark.parametrize("specs,expected", [
    ("Private Room in 3 bd / 2 ba", "private_room"),
    ("Shared Room in 1 bd / 1 ba", "shared_room"),
    ("Room in Studio / 1 ba", None),      # unlabelled -> escalate, don't guess
    ("2 bd / 1 ba", None),
    (None, None),
])
def test_room_type_from_unit_specs(specs, expected):
    assert _enrich.room_type_from_unit_specs(specs)[0] == expected


def test_contact_ignores_analytics_addresses():
    """A page-wide sweep pulled a real address out of a Snowplow comment."""
    markup = """
      <script>window.snowplow('setUserId', 'erica@sgrealestateco.com');</script>
      <!-- webmaster@example.com -->
      <a href="mailto:leasing@realco.com">Contact</a>
      <p>Call (510) 401-1803</p>
    """
    got = _enrich.extract_contact(markup)
    assert got["phone"] == "(510) 401-1803"
    assert got["emails"] == ["leasing@realco.com"]


def test_enrichment_merge_prefers_structured():
    structured = _enrich.Enrichment(room_type="private_room", method="structured",
                                    evidence={"room_type": "1 tenant max per room"})
    model = _enrich.Enrichment(room_type="shared_room", pets="none", method="llm")
    merged = structured.merge(model)
    assert merged.room_type == "private_room"   # structured wins
    assert merged.pets == "none"                # model fills the gap
    assert merged.method == "structured+llm"


# ── Cross-check: parser vs. detail page ──────────────────────────────────
#
# Two readers of the same facts from different sources. A disagreement is
# recorded, never auto-resolved -- the failure mode this catches is a
# confidently wrong value that nothing contradicts.

def _unit(**over):
    base = {"address": "1 A St, Berkeley, CA 94704", "bedrooms": 2.0,
            "bathrooms": 1.0, "rent": 999.0, "square_feet": 890,
            "available_date": "2026-08-10", "available_now": False,
            "city": "Berkeley", "kind": "residential"}
    base.update(over)
    return base


def _page(**over):
    base = {"describes_one_unit": True, "bedrooms": 2.0, "bathrooms": 1.0,
            "rent": 999.0, "square_feet": 890, "available_date": "2026-08-10",
            "available_now": False, "city": "Berkeley",
            "listing_kind": "residential"}
    base.update(over)
    return base


def test_crosscheck_agreement_is_silent():
    assert _enrich.compare_observed(_unit(), _page()) == []


@pytest.mark.parametrize("field,page_value", [
    ("bedrooms", 0.0),
    ("rent", 1450.0),
    ("city", "Oakland"),
    ("available_date", "2026-09-01"),
])
def test_crosscheck_flags_disagreement(field, page_value):
    found = _enrich.compare_observed(_unit(), _page(**{field: page_value}))
    assert [c["field"] for c in found] == [field]
    assert found[0]["type"] == "conflict"


def test_crosscheck_flags_kind_disagreement():
    """`kind` is the judgment layer and the largest source of defects."""
    found = _enrich.compare_observed(_unit(), _page(listing_kind="room"))
    assert found[0] == {"field": "kind", "parser": "residential",
                        "page": "room", "type": "conflict"}


def test_crosscheck_multi_unit_page_compares_nothing():
    """A building page cannot confirm any single unit's rent or bedrooms."""
    wild = _page(describes_one_unit=False, bedrooms=9.0, rent=1.0,
                 city="Nowhere", listing_kind="parking")
    assert _enrich.compare_observed(_unit(), wild) == []


def test_crosscheck_tolerates_rounded_square_feet():
    """"About 900 sq ft" vs 890 is not a defect worth a reviewer's time."""
    assert _enrich.compare_observed(_unit(square_feet=890),
                                    _page(square_feet=900)) == []


def test_crosscheck_flags_real_square_feet_gap():
    found = _enrich.compare_observed(_unit(square_feet=890), _page(square_feet=1400))
    assert [c["field"] for c in found] == ["square_feet"]


def test_crosscheck_city_case_insensitive():
    assert _enrich.compare_observed(_unit(city="San Francisco"),
                                    _page(city="san francisco")) == []


def test_crosscheck_reports_what_the_parser_missed():
    """A value the page states and the parser never captured is not an error."""
    found = _enrich.compare_observed(_unit(square_feet=None), _page(square_feet=900))
    assert found[0]["type"] == "parser_missing"
    assert found[0]["page"] == 900


def test_crosscheck_silent_page_is_not_a_conflict():
    """The page saying nothing is not disagreement."""
    silent = _page(bedrooms=None, rent=None, square_feet=None,
                   available_date=None, city=None, listing_kind="unknown")
    assert _enrich.compare_observed(_unit(), silent) == []
