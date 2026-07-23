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

    # ── AppFolio (East Bay / SF / Boston expansion) ───────────────────────
    # Added 2026-07; account verified by scraping a non-empty /listings.
    # Utopia Management is a national operator (300 units, 120 cities); it is
    # included because regions.py filters each manager's listings down to the
    # covered metros at merge time, so it contributes only its in-market units
    # rather than swamping the dataset.
    Source("utopia_management", "Utopia Management",
           "https://utopiamanagement.com/", "appfolio", "utopiamanagement"),
    Source("advent_properties", "Advent Properties",
           "https://adventpropertiesinc.com/", "appfolio", "adventproperties"),
    Source("all_east_bay", "All East Bay Properties",
           "https://alleastbayproperties.com/", "appfolio", "alleastbayproperties"),
    Source("bancal_pm", "BanCal Property Management",
           "https://www.bancalsf.com/", "appfolio", "bancalsf"),
    Source("bay_property_group", "Bay Property Group",
           "https://www.baypropertygroup.com/", "appfolio", "baypropertygroup"),
    Source("boston_property_care", "Boston Property Care",
           "https://www.bostonpropertycare.com/", "appfolio", "bostonpropertycare"),
    Source("cambridge_management_group", "Cambridge Management Group",
           "https://cambridgemgi.appfolio.com/", "appfolio", "cambridgemgi"),
    Source("chandler_properties", "Chandler Properties",
           "https://www.chandlerproperties.com/", "appfolio", "chandlerproperties"),
    Source("community_realty", "Community Realty Property Management",
           "https://www.crpmrealty.com/", "appfolio", "communityrealtypm"),
    Source("golden_gate_pm", "Golden State Property Management",
           "https://www.goldenstatepropertymanagement.com/", "appfolio", "goldenstate"),
    Source("ks_company", "K&S Company", "https://www.kands.com/",
           "appfolio", "kands"),
    Source("lapham_company", "Lapham Company", "https://www.laphamcompany.com/",
           "appfolio", "laphamcompany"),
    Source("professional_pm", "Professional Property Management",
           "https://www.ppm4rent.com/", "appfolio", "profpm"),
    Source("selborne_properties", "Selborne Properties",
           "https://www.selborneproperties.com/", "appfolio", "selborne"),
    Source("structure_boston", "Structure Boston",
           "https://structureboston.appfolio.com/", "appfolio", "structureboston"),
    Source("t_lee_development", "T. Lee Development",
           "https://tleedevelopment.appfolio.com/", "appfolio", "tleedevelopment"),
    Source("west_coast_pm", "West Coast Property Management",
           "https://wcpm.com/", "appfolio", "wcpm"),
    # Valid AppFolio account with no current vacancies (its /listings renders
    # "no available listings"); registered so the daily job picks up its
    # inventory when it lists.
    Source("cw_management", "CW Management",
           "https://www.cwmgmtinc.com/", "appfolio", "cwmgmt"),

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
    Source("boston_luxury", "Boston Luxury Real Estate",
           "https://bostonluxury.managebuilding.com/", "buildium", "bostonluxury"),
    # Valid Buildium accounts with no current vacancies ("no results" on the
    # public rentals page); registered so they populate when they list.
    Source("hawthorne_pmg", "Hawthorne Property Management Group",
           "https://hawthornepmg.managebuilding.com/", "buildium", "hawthornepmg"),
    Source("keyopp", "KeyOpp Property Management",
           "https://keyopp.net/", "buildium", "keyopp"),

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
    # AppFolio Websites (the hosted-site product, not the classic portal):
    # the page ships an `appfolio-listings` div and a base64 widget bundle
    # that fetches unit data from a runtime API at render time. The server
    # HTML and the ?format=json config both contain zero addresses or rents,
    # so there is no HTTP-only path -- only a headless browser would see the
    # listings. Their AppFolio *account* subdomain is not exposed either.
    Source("gb_management", "GB Management", "https://www.gbmboston.com/", "none",
           note="AppFolio Websites JS widget; listing data loads from a "
                "runtime API absent from the server HTML."),
    Source("gaetani", "Gaetani Real Estate",
           "https://www.gaetanirealestate.com/", "none",
           note="AppFolio Websites JS widget (same as gb_management)."),
    Source("maisel", "Maisel Property Management",
           "https://www.maiselpropertymanagement.com/", "none",
           note="AppFolio Websites JS widget (same as gb_management)."),
    # RentCafe / Yardi: the community pages hard-block non-browser requests
    # with HTTP 403, same wall as vindium above.
    Source("seven_cameron", "7 Cameron Apartments",
           "https://www.rentcafe.com/apartments/ma/cambridge/7-cameron/default.aspx",
           "none", note="RentCafe (Yardi); 403 to any non-browser client."),
    Source("chroma_apartments", "Chroma Apartments",
           "https://www.rentcafe.com/apartments/ma/cambridge/chroma/default.aspx",
           "none", note="RentCafe (Yardi); 403 to any non-browser client."),
    Source("rentsfnow", "RentSFNow / Veritas Investments",
           "https://www.rentsfnow.com/", "none",
           note="RentCafe (Yardi); 403 to any non-browser client."),
    Source("cl_boston", "CL Boston Property Management",
           "https://www.clbostonpropertymanagement.com/", "none",
           note="Rentvine single-page app; listings render client-side, "
                "nothing in the server HTML."),
]


def by_slug(slug: str) -> Source | None:
    for s in SOURCES:
        if s.slug == slug:
            return s
    return None


def scrapable_sources() -> list[Source]:
    return [s for s in SOURCES if s.scrapable]
