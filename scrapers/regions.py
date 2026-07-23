"""Which metros this aggregator covers, and the filter that enforces it.

The project started Bay-Area-only. Adding East Bay / SF / Boston / Cambridge
property managers pulled in some managers that are *regional or national*
operators -- one served 300 units across 120 cities, almost none in-market.
Rather than exclude a whole manager (and lose its in-market listings), the
filter here keeps a unit only when its city is in a covered metro. A national
operator therefore contributes exactly its Bay Area and Boston-metro units and
nothing else.

The lists are **allow-lists on purpose.** A deny-list would silently admit
every new out-of-market city a manager adds; an allow-list fails closed and
`merge.py` reports every city it dropped, so a legitimately-missing suburb is
visible and one line fixes it -- far better than junk quietly accumulating.

City names are matched case-insensitively and paired with state, so the
Belmont-CA / Belmont-MA collision (a real city in both metros) resolves
correctly.
"""
from __future__ import annotations

# ── Bay Area: the nine counties (Alameda, Contra Costa, Marin, Napa, San
# Francisco, San Mateo, Santa Clara, Solano, Sonoma). Kept broad so a new
# listing in an unremarkable suburb isn't dropped for being unlisted.
_BAY_AREA = {
    # San Francisco
    "san francisco",
    # Alameda County
    "oakland", "berkeley", "fremont", "hayward", "san leandro", "alameda",
    "emeryville", "albany", "piedmont", "san lorenzo", "castro valley",
    "cherryland", "ashland", "union city", "newark", "dublin", "pleasanton",
    "livermore", "sunol",
    # Contra Costa County
    "richmond", "concord", "walnut creek", "antioch", "pittsburg", "martinez",
    "san pablo", "el cerrito", "el sobrante", "pinole", "hercules", "brentwood",
    "oakley", "danville", "san ramon", "lafayette", "orinda", "moraga",
    "pleasant hill", "clayton", "kensington", "rodeo", "crockett", "bay point",
    # Marin County
    "san rafael", "novato", "mill valley", "sausalito", "larkspur",
    "corte madera", "tiburon", "san anselmo", "fairfax", "ross", "belvedere",
    "greenbrae",
    # Napa County
    "napa", "american canyon", "st helena", "calistoga", "yountville",
    # San Mateo County
    "san mateo", "redwood city", "daly city", "south san francisco",
    "san bruno", "pacifica", "burlingame", "millbrae", "foster city",
    "san carlos", "belmont", "menlo park", "east palo alto", "half moon bay",
    "brisbane", "colma", "hillsborough", "atherton", "woodside", "portola valley",
    "la honda",
    # Santa Clara County
    "san jose", "santa clara", "sunnyvale", "mountain view", "palo alto",
    "cupertino", "milpitas", "campbell", "los gatos", "saratoga", "gilroy",
    "morgan hill", "los altos", "los altos hills", "monte sereno",
    # Solano County
    "vallejo", "fairfield", "vacaville", "benicia", "suisun city", "dixon",
    "rio vista",
    # Sonoma County
    "santa rosa", "petaluma", "rohnert park", "windsor", "healdsburg",
    "sonoma", "sebastopol", "cotati", "cloverdale",
}

# ── Boston metro: Boston + Cambridge and the inner-ring suburbs, which is
# what "Boston / Cambridge" was asked for. Deliberately does NOT include the
# southeastern-Massachusetts cities some Boston managers also list (Fall
# River, New Bedford, Attleboro, Plymouth are 35-55 miles out) -- those show
# up in merge.py's drop report if they should be reconsidered.
_BOSTON_METRO = {
    "boston", "cambridge", "somerville", "brookline", "newton", "quincy",
    "medford", "malden", "everett", "chelsea", "revere", "watertown",
    "waltham", "arlington", "belmont", "winthrop", "milton", "dedham",
    "needham", "framingham", "natick", "brookline village",
    # Boston neighborhoods that parse as their own "city"
    "dorchester", "roxbury", "jamaica plain", "brighton", "allston",
    "roslindale", "west roxbury", "hyde park", "mattapan", "east boston",
    "south boston", "charlestown", "back bay", "beacon hill", "south end",
    "fenway", "mission hill", "north end",
}

# State -> covered-city set. A metro is identified by (state, city) so that
# same-named cities in different states don't leak across.
COVERED: dict[str, set[str]] = {
    "CA": _BAY_AREA,
    "MA": _BOSTON_METRO,
}


def in_covered_metro(city: str | None, state: str | None) -> bool:
    """True when a listing is in one of the covered metros.

    A unit with no city is treated as covered -- we can't geo-classify it,
    and dropping a real Berkeley listing over a malformed address is worse
    than keeping one ambiguous row. `merge.py` counts these separately.
    """
    if not city:
        return True
    cities = COVERED.get((state or "").upper())
    if cities is None:
        return False
    return city.strip().casefold() in cities
