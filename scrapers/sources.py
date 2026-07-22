"""Registry of every property manager we scrape.

Most Bay Area property managers don't build their own listing site -- they
rent one. Twenty of the sources below resolve to just two SaaS portals
(AppFolio and Buildium), which is why this project is a declarative registry
plus a handful of platform adapters rather than one bespoke scraper per
company. Adding another AppFolio manager is a single line here.

Each entry is a ``Source``:

    slug      snake_case id; becomes ``data/raw/<slug>.json``
    manager   human-readable company name, shown in the UI
    site      the manager's own public website (for attribution links)
    platform  which adapter in ``scrapers/platforms/`` handles it
    account   platform-specific handle -- for AppFolio and Buildium this is
              the portal subdomain, e.g. ``hayesmanagement`` resolves to
              https://hayesmanagement.appfolio.com/listings

``platform="none"`` records a manager we investigated but cannot currently
scrape, with ``note`` explaining why. They are kept in the registry rather
than deleted so the coverage table in the README stays honest and so a future
run can revisit them -- a site that has no vacancies today may list some next
month.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    slug: str
    manager: str
    site: str
    platform: str
    account: str | None = None
    note: str | None = None

    @property
    def scrapable(self) -> bool:
        return self.platform != "none"


SOURCES: list[Source] = [
    # ── AppFolio ──────────────────────────────────────────────────────────
    Source("ap_management", "AP Management", "https://apmanage.net/",
           "appfolio", "apmgmt"),
    Source("bay_cities_pm", "Bay Cities Property Management", "https://bcpm.com/",
           "appfolio", "baycitiespm"),
    Source("cedar_properties", "Cedar Properties",
           "https://www.cedarproperties.com/", "appfolio", "cedarpm"),
    # panoramicberkeley.com renders listings client-side from a Duda
    # collection; the AppFolio account behind it serves the same data as HTML.
    Source("panoramic_berkeley", "Panoramic Berkeley",
           "https://www.panoramicberkeley.com/", "appfolio", "panoramicmgt"),
    Source("authentic_properties", "Authentic Properties", "https://authenticre.com/",
           "appfolio", "authenticrealestate"),
    Source("beacon_properties", "Beacon Properties", "https://www.beacprop.com/",
           "appfolio", "beaconprop"),
    Source("boardwalk", "Boardwalk Properties", "https://www.boardwalkrents.com/",
           "appfolio", "boardwalkinvestments"),
    Source("california_pacific", "California Pacific Realty",
           "https://californiapacific.appfolio.com/listings",
           "appfolio", "californiapacific"),
    Source("gpmsf", "Gordon Property Management", "https://gpmsf.com/",
           "appfolio", "gordon"),
    Source("hayes_management", "Hayes Management", "https://hayesm.com/available/",
           "appfolio", "hayesmanagement"),
    Source("kasa_properties", "Kasa Properties", "https://www.kasaproperties.com/",
           "appfolio", "kasaproperties"),
    Source("korman_and_ng", "Korman & Ng Real Estate Services",
           "https://www.kormanandngpm.com/", "appfolio", "kormanandng"),
    Source("myerhoff", "Myerhoff & Associates",
           "https://www.bayapartmentbroker.com/", "appfolio", "myerhoff"),
    Source("north_berkeley_properties", "North Berkeley Properties",
           "https://www.northberkeleyproperties.com/",
           "appfolio", "northberkeleyproperties"),
    Source("perkins_realty", "Perkins Realty Partners",
           "https://perkinsrealtypartners.com/", "appfolio", "perkins"),
    # The public sgreresidential.com site is a WordPress brochure that links
    # out; the inventory lives on this AppFolio account.
    Source("sg_real_estate", "SG Real Estate Co.", "https://sgreresidential.com/",
           "appfolio", "sgrealestate"),
    Source("structure_properties", "Structure Properties",
           "https://structureproperties.com/", "appfolio", "spsf"),
    Source("two_b_living", "2B Living", "https://www.twobliving.com/",
           "appfolio", "twobliving"),

    # ── Buildium ──────────────────────────────────────────────────────────
    Source("academic_housing_rentals", "Academic Housing Rentals",
           "https://www.academichousingrentals.com",
           "buildium", "academichousingrentals"),
    Source("collabhome", "CollabHome", "https://rentals.collabhome.io/",
           "buildium", "collab"),
    Source("domingo_properties", "Domingo Properties",
           "https://www.domingoprop.com/", "buildium", "domingoprop"),
    Source("mpm_oakland", "MPM Oakland",
           "https://mpmoakland.managebuilding.com/", "buildium", "mpmoakland"),
    Source("raj_properties", "Raj Properties", "https://www.rajproperties.com/",
           "buildium", "rajproperties"),
    Source("square_one_management", "Square One Management",
           "https://squareonemanagement.com/", "buildium", "som"),

    # ── Own platform / bespoke ────────────────────────────────────────────
    Source("tripalink", "Tripalink",
           "https://tripalink.com/berkeley/homes-for-rent", "tripalink"),
    Source("relisto", "ReListo", "https://www.relisto.com/", "relisto"),
    Source("eri_property_management", "ERI Property Management",
           "https://www.erirentals.com/", "eri"),
    Source("mmg_properties", "MMG Properties", "https://mmgprop.com/", "mmg"),
    Source("boston_pm", "Boston Property Management",
           "https://bostonpropertymanagementllc.com/", "boston_pm"),
    # RentManager portal is login-only; the public feed is ShowMojo.
    Source("premium_properties", "Premium Properties", "https://premiumpd.com/",
           "showmojo", "a2a2489044"),

    # ── Investigated, not currently scrapable ─────────────────────────────
    # Kept here so the coverage table stays honest and a future run can
    # revisit them. See README "Sources we can't scrape" for detail.
    Source("hudson_mcdonald", "Hudson McDonald Properties",
           "https://hudsonmcdonald.com/", "none",
           note="Publishes no unit-level inventory. Its Buildium public site "
                "is disabled (redirects to the tenant login) and the WordPress "
                "site has no listing post type -- only per-building marketing "
                "pages quoting 'from $X' floor-plan prices with no addresses, "
                "unit numbers or move-in dates."),
    Source("berkeley_group", "The Berkeley Group", "https://tbgpm.com/", "none",
           note="Entrata ProspectPortal at properties.tbgpm.com behind a "
                "Cloudflare managed challenge. Scrapable only with a headless "
                "browser from a residential IP, which is not dependable from "
                "GitHub Actions runners. ~59 available student-housing "
                "floorplans across 21 Berkeley properties are visible when "
                "the challenge clears."),
    Source("vindium", "Vindium Real Estate",
           "https://www.vindiumrealestate.com/", "none",
           note="Yardi RentCafe behind two Cloudflare layers: the site itself "
                "serves a managed JS challenge, and rentcafe.com/securecafe.com "
                "hard-block datacenter IPs outright. No HTTP-only path exists."),
    Source("oxford_apartments", "Oxford Property Management",
           "https://www.oxford-apartments.com/", "none",
           note="Single hand-authored Wix page listing one vacancy. Bedrooms "
                "and availability date are present but rent is deliberately "
                "withheld sitewide (zero dollar amounts in 776 KB of HTML) -- "
                "the CTA is a phone number, so price cannot be recovered."),
]


def by_slug(slug: str) -> Source | None:
    for s in SOURCES:
        if s.slug == slug:
            return s
    return None


def scrapable_sources() -> list[Source]:
    return [s for s in SOURCES if s.scrapable]
