"""Second-pass enrichment: amenities, room type, contact, and summaries.

The adapters in ``platforms/`` read a manager's *index* page, which carries
the headline facts (address, beds, rent, date). Amenities and room type are
almost never on the index -- they live on each listing's detail page, in
prose. This module is the second pass.

Two extraction tiers, and deliberately not three:

1. **Structured fields already in hand** (free, deterministic, no request).
   Tripalink's property JSON -- which ``tripalink.py`` already fetches --
   carries ``leasePolicy`` with ``Pet Policy: ["No Pets"]``, and AppFolio's
   listing markup carries ``Bed / Bath: "Shared Room in 1 bd / 1 ba"``.
   Reading a labelled field out of a payload we already hold is not
   pattern-matching prose, and it costs nothing.

2. **The model** (``llm.py``), for everything else.

There used to be a middle tier: ~180 lines of ordered regex over detail-page
text, where "no parking" had to be tested before "parking" and "unfurnished"
before "furnished". It was deleted, for two reasons.

*It wasn't saving anything.* Every listing's detail page is fetched anyway to
write its summary, and the input tokens dominate the cost of that call.
Asking the same request for six more fields costs about ten cents across the
whole dataset. The regex tier was buying nothing.

*It was where the bugs were.* Every defect found while building this --
"2 tenants max per room" read as a shared bedroom, 26 rooms written as whole
apartments, 19 "BED IN TRIPLE" beds counted as apartments -- came from
pattern rules, not from the model. And a wrong pattern fails silently: you
get ``parking="garage"`` with nothing to inspect. A model answer arrives with
``evidence`` quoting the sentence it used, which is the thing you actually
need when a value looks wrong.

What survives from that tier is its hard-won knowledge, moved into the
prompt in ``llm.py`` -- the phrasings that are routinely misread are listed
there explicitly, because the difficulty was never the mechanism. It was
deciding what the field means. "2 tenants max per room" fooled a regex and it
fooled the model too; what fixed it was pinning down that a capacity ceiling
is not a statement about who occupies the room.
"""
from __future__ import annotations

import html as _html
import json
import re
import time as _time
from dataclasses import dataclass
from pathlib import Path

from base import clean_text, parse_available, polite_get

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = REPO_ROOT / "data" / "enrich_cache.json"

# Fetched page text, kept only so an interrupted run resumes instead of
# re-fetching 568 pages. Not committed: it is several MB of scraped markup
# with no archival value, and it goes stale by design (see PAGE_CACHE_TTL_H).
PAGE_CACHE_PATH = REPO_ROOT / "data" / ".page_cache.json"
PAGE_CACHE_TTL_H = 24

# Fields this module fills in. `room_type` is here too because the same
# detail-page text answers it.
ENRICHED_FIELDS = ("room_type", "pets", "furnished", "parking", "laundry", "summary")


@dataclass
class Enrichment:
    """What one listing's structured fields and detail page yielded.

    ``method`` and ``evidence`` exist so a wrong value in a committed raw
    file can be traced back to the text that produced it. Without them an
    unexplained flip between daily runs is undebuggable.
    """

    room_type: str | None = None
    pets: str | None = None
    furnished: str | None = None
    parking: str | None = None
    laundry: str | None = None
    summary: str | None = None
    method: str = "none"           # structured | llm | structured+llm | none
    evidence: dict | None = None   # field -> quoted source text

    def merge(self, other: "Enrichment") -> "Enrichment":
        """Overlay *other*, keeping self's non-null values.

        Called as ``structured.merge(llm)``: a labelled field read from a
        payload we already hold always beats a reading of prose.
        """
        ev = dict(self.evidence or {})
        ev.update(other.evidence or {})
        methods = [m for m in (self.method, other.method) if m != "none"]
        return Enrichment(
            **{
                f: getattr(self, f) if getattr(self, f) is not None else getattr(other, f)
                for f in ENRICHED_FIELDS
            },
            method="+".join(dict.fromkeys(methods)) or "none",
            evidence=ev or None,
        )

    def missing(self) -> list[str]:
        return [f for f in ENRICHED_FIELDS if getattr(self, f) is None]

    def is_complete(self) -> bool:
        return not self.missing()


# ── Structured fields already in hand ─────────────────────────────────────

_OCCUPANCY_RE = re.compile(r"(\d+)\s*tenants?\s*max\s*per\s*room", re.I)


def room_type_from_occupancy(
    lease_policy: list | None, *, is_room: bool
) -> tuple[str | None, str | None]:
    """Read Tripalink's ``Occupancy Policy`` catalog entry.

    ``[{"catalogName": "Occupancy Policy", "type": ["2 tenants max per room"]}]``

    **Only a cap of 1 resolves anything.** "1 tenant max per room" forecloses
    sharing: nobody else can be placed in the room, so it is private. Any
    higher cap is genuinely undetermined and returns None.

    The temptation is to read the cap as an answer, and both directions are
    wrong:

    * ``N>=2 -> shared_room`` was the first attempt. It would have
      mislabelled 110 listings, and is the same misreading that makes
      "PRIVATE ROOM ... rate is per room for double occupancy" look
      self-contradictory when it is not.
    * ``N>=2 -> private_room`` is equally unfounded, and is what the model
      answered when asked cold. That the operator lists and prices one room
      at a time proves the room is the leased unit; it says nothing about who
      ends up inside it. An operator running roommate matching with a
      2-tenant cap may mean the renter's partner or a stranger, and the
      listing does not distinguish them.

    A genuine shared room says so about the *bed*, not the room -- "rent by
    the bed", "a bed in a double", "shared bed space" -- and is listed per
    bed (``Room 2 Bed A``/``Bed B``, ``Dwight 7A``/``7B``). Absent that, the
    honest value is None.

    Gated on ``is_room``: on a whole apartment the same policy is about total
    headcount, not bedroom sharing. Returns ``(room_type, evidence)``.
    """
    if not is_room or not lease_policy:
        return None, None
    for policy in lease_policy:
        if not isinstance(policy, dict):
            continue
        if (policy.get("catalogName") or "").strip().lower() != "occupancy policy":
            continue
        for entry in policy.get("type") or []:
            m = _OCCUPANCY_RE.search(str(entry))
            if m:
                if int(m.group(1)) == 1:
                    return "private_room", str(entry)
                return None, None       # cap >= 2: undetermined
    return None, None


_NO_PETS_RE = re.compile(r"\bno\s+pets?\b", re.I)
_CATS_RE = re.compile(r"\bcats?\b", re.I)
_DOGS_RE = re.compile(r"\bdogs?\b", re.I)


def pets_from_policy(lease_policy: list | None) -> tuple[str | None, str | None]:
    """Read Tripalink's ``Pet Policy`` catalog entry.

    The values here are a short controlled list set by the operator ("No
    Pets", "Cats Allowed"), not prose, which is why reading them directly is
    safe where scanning a page body would not be.
    """
    if not lease_policy:
        return None, None
    for policy in lease_policy:
        if not isinstance(policy, dict):
            continue
        if (policy.get("catalogName") or "").strip().lower() != "pet policy":
            continue
        text = " ".join(str(t) for t in (policy.get("type") or []))
        if not text.strip():
            continue
        if _NO_PETS_RE.search(text):
            return "none", text
        cats, dogs = bool(_CATS_RE.search(text)), bool(_DOGS_RE.search(text))
        if cats and dogs:
            return "allowed", text
        if cats:
            return "cats_only", text
        if dogs:
            return "dogs_only", text
    return None, None


# AppFolio states both facts in one element: "Private Room in 3 bd / 2 ba".
# The leading word is missing on some ("Room in Studio / 1 ba"), which is why
# this returns None rather than defaulting -- an unlabelled room is exactly
# the case that should reach the model.
_UNIT_SPECS_RE = re.compile(r"^\s*(private|shared)\s+room\s+in\b", re.I)


def room_type_from_unit_specs(unit_specs: str | None) -> tuple[str | None, str | None]:
    """Read AppFolio's bed/bath element when it names the room type."""
    if not unit_specs:
        return None, None
    m = _UNIT_SPECS_RE.search(unit_specs)
    if not m:
        return None, None
    return f"{m.group(1).lower()}_room", unit_specs.strip()


# ── Contact details ───────────────────────────────────────────────────────
#
# Manager-level, not per-unit: the same office number appears on all 164 of
# one manager's listings, so it is collected once per source and written to a
# `managers` block rather than copied onto every record.

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_MAILTO_RE = re.compile(r'mailto:([\w.+-]+@[\w-]+\.[\w.]{2,})', re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b")

_EMAIL_DENY_RE = re.compile(
    r"@(example|sentry|snowplow|googlemail|localhost)"
    r"|(noreply|no-reply|donotreply|support@wordpress|webmaster)@"
    r"|\.(png|jpg|gif|css|js)$",
    re.I,
)


def extract_contact(markup: str) -> dict:
    """Pull leasing phone + emails from a listing or contact page.

    ``<script>``, ``<style>`` and HTML comments are stripped *before*
    matching. A page-wide sweep of one AppFolio detail page returned
    ``erica@sgrealestateco.com`` out of a Snowplow analytics comment --
    syntactically valid, not a leasing contact. ``mailto:`` links are
    preferred over bare text for the same reason.
    """
    if not markup:
        return {"phone": None, "emails": []}

    body = _COMMENT_RE.sub(" ", _SCRIPT_RE.sub(" ", markup))
    text = clean_text(body)

    emails: list[str] = []
    for candidate in _MAILTO_RE.findall(body) + _EMAIL_RE.findall(text):
        addr = _html.unescape(candidate).strip().lower().rstrip(".")
        if _EMAIL_DENY_RE.search(addr) or addr in emails:
            continue
        emails.append(addr)

    phone = None
    m = _PHONE_RE.search(text)
    if m:
        phone = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"

    return {"phone": phone, "emails": emails}


# ── Cross-checking the parser against the page ────────────────────────────
#
# The parser reads a manager's index page; the model reads the listing's own
# detail page. Two readers, two sources, same facts -- so a disagreement is
# information. Nothing here overwrites the parser: a conflict is recorded and
# surfaced, because the failure mode this exists to catch is a confidently
# wrong value that nothing contradicts.
#
# Tolerances are per-field rather than exact equality, since the two sources
# legitimately round differently. Square footage advertised as "about 900" on
# one page and 890 on the other is not a defect worth a reviewer's time.

_TOLERANCE = {
    "rent": 1.0,            # cents-level drift between a range and a headline
    "bathrooms": 0.01,
    "bedrooms": 0.0,        # a bedroom count is discrete; any gap is real
    "square_feet": 0.0,     # handled proportionally below
}
_SQFT_RELATIVE = 0.02       # 2% -- "approximately 900 sq ft" vs 890


def compare_observed(unit: dict, observed: dict | None) -> list[dict]:
    """Compare parser output against the model's independent read.

    Returns a list of ``{field, parser, page, type}`` records. ``type`` is
    ``conflict`` when both readers produced a value and they disagree, or
    ``parser_missing`` when the page states something the parser never
    captured -- the second is not an error, but it is where the parser is
    leaving data on the table.

    Returns nothing at all when the page does not describe exactly one unit.
    A building page listing eleven floor plans cannot confirm any single
    unit's rent, and comparing against it would produce conflicts that are
    really just the wrong question asked.
    """
    if not observed or not observed.get("describes_one_unit"):
        return []

    out: list[dict] = []
    for field in ("bedrooms", "bathrooms", "rent", "square_feet",
                  "available_date", "available_now", "city"):
        page = observed.get(field)
        mine = unit.get(field)
        if page is None or page == "":
            continue
        if mine is None or mine == "":
            # available_now defaults to False rather than being absent, so a
            # False-vs-False pair is agreement, not a missing value.
            if field == "available_now" and page is False:
                continue
            out.append({"field": field, "parser": mine, "page": page,
                        "type": "parser_missing"})
            continue

        if field in ("bedrooms", "bathrooms", "rent"):
            try:
                if abs(float(mine) - float(page)) > _TOLERANCE[field]:
                    out.append({"field": field, "parser": mine, "page": page,
                                "type": "conflict"})
            except (TypeError, ValueError):
                continue
        elif field == "square_feet":
            try:
                if abs(float(mine) - float(page)) > max(5.0, float(mine) * _SQFT_RELATIVE):
                    out.append({"field": field, "parser": mine, "page": page,
                                "type": "conflict"})
            except (TypeError, ValueError):
                continue
        elif field == "available_date":
            # Normalise both sides before comparing. The model reports what
            # the page shows ("8/10/26", "09/01/2026"); the parser stores
            # ISO. Comparing the raw strings makes every date look like a
            # disagreement -- 103 of the first run's 248 "conflicts" were
            # this, pure formatting noise that buries the real ones.
            page_iso, _ = parse_available(str(page))
            if page_iso and page_iso != mine:
                out.append({"field": field, "parser": mine, "page": page_iso,
                            "type": "conflict"})
        elif field == "city":
            if str(mine).strip().casefold() != str(page).strip().casefold():
                out.append({"field": field, "parser": mine, "page": page,
                            "type": "conflict"})
        elif mine != page:
            out.append({"field": field, "parser": mine, "page": page,
                        "type": "conflict"})

    # `kind` is the judgment layer, and the one that has produced the most
    # defects -- 19 "BED IN TRIPLE" beds counted as whole apartments, a studio
    # classified as a parking stall. Worth comparing on its own terms.
    page_kind = observed.get("listing_kind")
    if page_kind and page_kind != "unknown" and unit.get("kind"):
        if page_kind != unit["kind"]:
            out.append({"field": "kind", "parser": unit["kind"], "page": page_kind,
                        "type": "conflict"})
    return out


def conflicts_from_cache(raw: dict, cache: dict) -> tuple[list, dict]:
    """Recompute parser/page disagreements from the URL-keyed cache.

    URL-keyed throughout, so it is stable across re-scrapes -- the earlier
    positional approach compared batch id ``p00042`` against the 42nd unit of
    whatever scrape happened to be on disk, which drifted the moment the URL
    set changed. Returns ``(rows, counts)``.
    """
    import collections
    counts = collections.Counter()
    rows = []
    for slug, doc in raw.items():
        members = collections.defaultdict(list)
        for u in doc.get("units", []):
            if u.get("url") and u.get("kind") in ("residential", "room"):
                members[u["url"]].append(u)
        for url, units in members.items():
            if len(units) != 1:
                continue                     # multi-unit page: can't confirm
            hit = cache.get(url)
            if not hit or not hit.get("observed"):
                continue
            for c in compare_observed(units[0], hit["observed"]):
                counts[c["type"]] += 1
                if c["type"] == "conflict":
                    rows.append({"address": units[0].get("address"),
                                 "manager": doc.get("manager"),
                                 "url": url, **c})
    return sorted(rows, key=lambda r: (r["field"], r["address"] or "")), counts


# ── Detail pages and cache ────────────────────────────────────────────────


def load_page_cache() -> dict:
    """Page text from a recent run, for resuming an interrupted fetch.

    Entries older than PAGE_CACHE_TTL_H are dropped on load: this exists to
    survive a killed process within one day's run, not to avoid re-reading
    listings that may have changed. A daily job therefore always sees fresh
    pages, while a retry minutes later costs nothing.
    """
    if not PAGE_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(PAGE_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = _time.time() - PAGE_CACHE_TTL_H * 3600
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and v.get("at", 0) >= cutoff}


def save_page_cache(cache: dict) -> None:
    PAGE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGE_CACHE_PATH.write_text(json.dumps(cache))


def fetch_detail_text(url: str, *, max_chars: int = 20_000) -> str | None:
    """Fetch a listing detail page and reduce it to model-readable text.

    Truncated because Tripalink's page is 189k characters, most of it SEO
    navigation ("Homes with Private Bathroom for rent near UC Berkeley")
    that misleads any reader, human or otherwise. The listing's own facts sit
    near the top. This cap is also what keeps the per-listing token cost --
    and therefore the whole project's LLM spend -- predictable.
    """
    try:
        markup = polite_get(url)
    except Exception:
        return None
    body = _COMMENT_RE.sub(" ", _SCRIPT_RE.sub(" ", markup))
    return re.sub(r"\s+", " ", clean_text(body))[:max_chars]


def cache_key(url: str | None) -> str:
    """Identity of a model call: the page it reads.

    The URL alone, deliberately. The request's entire input is the detail
    page, so two units pointing at the same page have the same answer --
    Tripalink's 271 units resolve to 11 property pages, and asking about each
    unit separately would buy 260 identical responses.

    Rent and title are *not* part of the key even though they change more
    often. They don't alter the page the model reads, and the prompt forbids
    repeating price in the summary, so folding them in would re-spend on
    every price move for an identical answer. It also means a listing that is
    merely still listed is never re-sent, which is what holds daily spend to
    the handful of genuinely new pages -- and what stops a committed raw file
    from flip-flopping its summary between runs. An unstable diff is the
    thing the repo's sort order already exists to prevent.

    The tradeoff: a page rewritten in place, at the same URL, is not noticed.
    Clearing data/enrich_cache.json forces a rebuild when that matters.
    """
    return url or ""


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}   # a corrupt cache costs money to rebuild, never correctness


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))
