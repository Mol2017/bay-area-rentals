/* Bay Area Rentals dashboard.
 *
 * Reads data/merged.json (produced by scripts/merge.py), renders the filter
 * bar, three charts and the listings table, and re-renders everything from a
 * single filter state object.
 *
 * Charts are plain DOM (divs sized by percentage) rather than SVG or a chart
 * library. At this data size the bars are simple enough that DOM keeps the
 * page dependency-free and lets CSS own the light/dark theming -- the same
 * custom properties drive the marks and the chrome.
 */
(function () {
  "use strict";

  var DATA_URL = "../data/merged.json";
  var PAGE_SIZE = 50;

  var state = {
    all: [],
    meta: null,
    managers: {},
    filtered: [],
    shown: PAGE_SIZE,
    filters: {
      city: "",
      beds: "",
      manager: "",
      maxRent: null,
      byDate: "",
      wholeOnly: false,
      roomType: "",
      pets: "",
      furnished: "",
      parking: "",
      laundry: "",
      sort: "soonest"
    }
  };

  // ── helpers ────────────────────────────────────────────────────────────

  function $(id) { return document.getElementById(id); }

  function money(v) {
    if (v === null || v === undefined) return "—";
    return "$" + Math.round(v).toLocaleString("en-US");
  }

  /* A per-room listing's bedroom count describes the floor plan it sits in,
     not what you rent -- labelling an $810 room in a 3-bedroom flat as
     "3 bd" (or a 0-bedroom SRO as "Studio") would misread as a whole
     apartment. Rooms get their own label and cite the floor plan instead.

     Four states, not three: room_type may be null on a listing that IS a
     room but whose source never said whether the bedroom is shared. That
     renders as a bare "Room", never as a whole unit -- calling it "3 bd"
     would republish a guess as a fact. */
  function isWholeUnit(u) { return u.room_type === "entire_place"; }

  function bedLabel(u) {
    var v = u.bedrooms;
    if (!isWholeUnit(u)) {
      var what = u.room_type === "shared_room" ? "Shared room"
               : u.room_type === "private_room" ? "Private room"
               : "Room";
      return v ? what + " in " + v + " bd" : what;
    }
    if (v === null || v === undefined) return "—";
    if (v === 0) return "Studio";
    return v + " bd";
  }

  /* Amenity values are nullable and the null means "the source didn't say",
     which is a different claim from the negative value. `parking: "none"` is
     the listing stating there is no parking; null is silence. Rendering both
     as "No parking" would invent information. */
  var AMENITY_LABELS = {
    pets: { allowed: "Pets OK", cats_only: "Cats OK", dogs_only: "Dogs OK", none: "No pets" },
    furnished: { furnished: "Furnished", partial: "Part furnished", unfurnished: "Unfurnished" },
    parking: { garage: "Garage", off_street: "Off-street parking", street: "Street parking", none: "No parking" },
    laundry: { in_unit: "In-unit laundry", shared: "Shared laundry", hookups: "Laundry hookups", none: "No laundry" }
  };

  function amenityTags(u) {
    var out = [];
    ["laundry", "parking", "furnished", "pets"].forEach(function (f) {
      var v = u[f];
      if (v && AMENITY_LABELS[f][v]) {
        out.push({ field: f, value: v, label: AMENITY_LABELS[f][v] });
      }
    });
    return out;
  }

  function median(nums) {
    if (!nums.length) return null;
    var s = nums.slice().sort(function (a, b) { return a - b; });
    var mid = Math.floor(s.length / 2);
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  }

  /* Dates in the feed are plain YYYY-MM-DD calendar dates with no timezone.
     `new Date("2026-08-01")` parses as UTC midnight and then renders as
     July 31 for anyone west of Greenwich -- which is every user of this
     site. Splitting the parts and using the local-time constructor keeps
     the date the manager published. */
  function parseISODate(iso) {
    if (!iso) return null;
    var p = String(iso).split("-");
    if (p.length !== 3) return null;
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmtDate(iso) {
    var d = parseISODate(iso);
    if (!d) return null;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function monthKey(iso) {
    var d = parseISODate(iso);
    if (!d) return null;
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
  }

  function monthLabel(key) {
    var p = key.split("-");
    var d = new Date(+p[0], +p[1] - 1, 1);
    return d.toLocaleDateString("en-US", { month: "short", year: "2-digit" });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ── filtering ──────────────────────────────────────────────────────────

  function applyFilters() {
    var f = state.filters;
    var cutoff = f.byDate ? parseISODate(f.byDate) : null;

    state.filtered = state.all.filter(function (u) {
      if (f.city && u.city !== f.city) return false;
      if (f.manager && u.manager !== f.manager) return false;
      if (f.wholeOnly && !isWholeUnit(u)) return false;
      if (f.roomType && u.room_type !== f.roomType) return false;

      // An amenity filter matches only listings that state the value. A
      // listing the source was silent about is excluded rather than assumed
      // either way -- the count shown is "known to have this", not "might".
      if (f.pets && u.pets !== f.pets) return false;
      if (f.furnished && u.furnished !== f.furnished) return false;
      if (f.parking && u.parking !== f.parking) return false;
      if (f.laundry && u.laundry !== f.laundry) return false;

      if (f.beds !== "") {
        // The "4+" bucket collapses everything at or above 4 bedrooms.
        if (f.beds === "4plus") {
          if (u.bedrooms === null || u.bedrooms === undefined || u.bedrooms < 4) return false;
        } else if (String(u.bedrooms) !== f.beds) {
          return false;
        }
      }

      if (f.maxRent !== null) {
        // A unit with no published rent can't be excluded by a rent filter
        // without hiding real inventory, so it stays visible.
        if (u.rent !== null && u.rent !== undefined && u.rent > f.maxRent) return false;
      }

      if (cutoff) {
        if (u.available_now) return true;
        var d = parseISODate(u.available_date);
        // No date published -> keep it; the renter can still enquire.
        if (d && d > cutoff) return false;
      }

      return true;
    });

    sortFiltered();
    state.shown = PAGE_SIZE;
  }

  function sortFiltered() {
    var mode = state.filters.sort;
    var big = 9e9;

    state.filtered.sort(function (a, b) {
      switch (mode) {
        case "rent-asc":
          return (a.rent === null ? big : a.rent) - (b.rent === null ? big : b.rent);
        case "rent-desc":
          return (b.rent === null ? -1 : b.rent) - (a.rent === null ? -1 : a.rent);
        case "beds-desc":
          return (b.bedrooms === null ? -1 : b.bedrooms) - (a.bedrooms === null ? -1 : a.bedrooms);
        case "city":
          return String(a.city || "~").localeCompare(String(b.city || "~"));
        default: {
          // Soonest move-in: available-now first, then by date, then rent.
          if (a.available_now !== b.available_now) return a.available_now ? -1 : 1;
          var ad = a.available_date || "9999-99-99";
          var bd = b.available_date || "9999-99-99";
          if (ad !== bd) return ad < bd ? -1 : 1;
          return (a.rent === null ? big : a.rent) - (b.rent === null ? big : b.rent);
        }
      }
    });
  }

  // ── charts ─────────────────────────────────────────────────────────────

  function renderHBar(container, rows, fmt) {
    container.innerHTML = "";
    if (!rows.length) {
      container.appendChild(el("p", "chart__empty", "No units match these filters."));
      return;
    }
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; }));
    var wrap = el("div", "hbar");

    rows.forEach(function (r) {
      var row = el("div", "hbar__row");
      row.appendChild(el("div", "hbar__name", r.name));

      var track = el("div", "hbar__track");
      var fill = el("div", "hbar__fill");
      fill.style.width = max > 0 ? (r.value / max) * 100 + "%" : "0%";
      track.appendChild(fill);
      row.appendChild(track);

      row.appendChild(el("div", "hbar__val", fmt ? fmt(r.value) : r.value));
      row.title = r.name + ": " + (fmt ? fmt(r.value) : r.value);
      wrap.appendChild(row);
    });

    container.appendChild(wrap);
  }

  function renderVBar(container, cols, fmt, altIndexFn) {
    container.innerHTML = "";
    if (!cols.length) {
      container.appendChild(el("p", "chart__empty", "No units match these filters."));
      return;
    }
    var max = Math.max.apply(null, cols.map(function (c) { return c.value; }));
    var wrap = el("div", "vbar");

    cols.forEach(function (c, i) {
      var col = el("div", "vbar__col");
      var bar = el("div", "vbar__bar" + (altIndexFn && altIndexFn(c, i) ? " vbar__bar--alt" : ""));
      // Reserve room for the value label above the tallest bar.
      bar.style.height = max > 0 ? Math.max((c.value / max) * 150, 2) + "px" : "2px";
      col.appendChild(el("div", "vbar__val", fmt ? fmt(c.value) : c.value));
      col.appendChild(bar);
      col.appendChild(el("div", "vbar__lab", c.label));
      col.title = c.label + ": " + (fmt ? fmt(c.value) : c.value);
      wrap.appendChild(col);
    });

    container.appendChild(wrap);
  }

  function renderCharts() {
    var units = state.filtered;

    // 1. Units by city (top 12).
    var byCity = {};
    units.forEach(function (u) {
      if (!u.city) return;
      byCity[u.city] = (byCity[u.city] || 0) + 1;
    });
    var cityRows = Object.keys(byCity)
      .map(function (k) { return { name: k, value: byCity[k] }; })
      .sort(function (a, b) { return b.value - a.value; })
      .slice(0, 12);
    renderHBar($("chart-city"), cityRows);

    // 2. Median rent by bedroom count. Whole units only -- mixing per-room
    //    co-living prices into a "3 bed" median would understate it badly.
    var byBed = {};
    units.forEach(function (u) {
      if (!isWholeUnit(u)) return;
      if (u.rent === null || u.rent === undefined) return;
      if (u.bedrooms === null || u.bedrooms === undefined) return;
      // Floor half-bedrooms into their integer bucket: a "1.5 bd" is a
      // one-bedroom-plus-den, and leaving it as its own bucket produced a
      // single-listing column whose median read as a real market rate.
      var k = Math.min(Math.floor(u.bedrooms), 4);
      (byBed[k] = byBed[k] || []).push(u.rent);
    });
    var bedCols = Object.keys(byBed)
      .map(Number)
      .sort(function (a, b) { return a - b; })
      .map(function (k) {
        return {
          label: (k === 0 ? "Studio" : (k >= 4 ? "4+ bd" : k + " bd")) +
                 " (" + byBed[k].length + ")",
          value: Math.round(median(byBed[k])),
          n: byBed[k].length
        };
      });
    renderVBar($("chart-rent"), bedCols, money);

    // 3. Availability by month. "Available now" is its own leading column
    //    (drawn in the secondary hue) because it is a different claim from a
    //    dated future opening, not just an earlier one.
    var nowCount = 0;
    var byMonth = {};
    units.forEach(function (u) {
      if (u.available_now) { nowCount++; return; }
      var k = monthKey(u.available_date);
      if (!k) return;
      byMonth[k] = (byMonth[k] || 0) + 1;
    });
    var monthCols = Object.keys(byMonth).sort().slice(0, 11).map(function (k) {
      return { label: monthLabel(k), value: byMonth[k] };
    });
    if (nowCount) monthCols.unshift({ label: "Now", value: nowCount, isNow: true });
    renderVBar($("chart-timeline"), monthCols, null, function (c) { return !!c.isNow; });
  }

  // ── table ──────────────────────────────────────────────────────────────

  function renderTable() {
    var tbody = $("unit-tbody");
    tbody.innerHTML = "";

    var slice = state.filtered.slice(0, state.shown);

    slice.forEach(function (u) {
      var tr = el("tr");

      // Address (+ link, + any per-unit caveat such as per-room pricing)
      var tdAddr = el("td");
      var wrap = el("div", "addr");
      if (u.url) {
        var a = el("a", null, u.address);
        a.href = u.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        wrap.appendChild(a);
      } else {
        wrap.appendChild(el("span", null, u.address));
      }
      if (u.summary) {
        wrap.appendChild(el("p", "addr__summary", u.summary));
      }
      var tags = amenityTags(u);
      if (tags.length) {
        var tagRow = el("div", "amenities");
        tags.forEach(function (t) {
          tagRow.appendChild(
            el("span", "amenity amenity--" + t.field +
               (t.value === "none" ? " amenity--absent" : ""), t.label)
          );
        });
        wrap.appendChild(tagRow);
      }
      var contact = state.managers[u.manager];
      if (contact && (contact.phone || (contact.emails || []).length)) {
        var c = el("div", "contact");
        if (contact.phone) {
          var tel = el("a", "contact__link", contact.phone);
          tel.href = "tel:" + contact.phone.replace(/[^0-9]/g, "");
          c.appendChild(tel);
        }
        (contact.emails || []).slice(0, 2).forEach(function (addr) {
          var m = el("a", "contact__link", addr);
          m.href = "mailto:" + addr;
          c.appendChild(m);
        });
        wrap.appendChild(c);
      }
      tdAddr.appendChild(wrap);
      tr.appendChild(tdAddr);

      tr.appendChild(el("td", null, u.city || "—"));

      var tdBeds = el("td", "num");
      // Muted only when there is genuinely nothing to say. A room with no
      // bedroom count still renders a real label ("Room"), so it is not
      // greyed out the way an unknown whole-unit bed count is.
      tdBeds.appendChild(el("span", u.bedrooms === null && isWholeUnit(u) ? "muted" : null, bedLabel(u)));
      tr.appendChild(tdBeds);

      var tdRent = el("td", "num");
      tdRent.appendChild(el("span", u.rent === null ? "muted" : null, money(u.rent)));
      tr.appendChild(tdRent);

      var tdAvail = el("td");
      if (u.available_now) {
        tdAvail.appendChild(el("span", "tag tag--now", "Now"));
      } else if (u.available_date) {
        tdAvail.appendChild(el("span", null, fmtDate(u.available_date)));
      } else {
        tdAvail.appendChild(el("span", "muted", "Not listed"));
      }
      tr.appendChild(tdAvail);

      tr.appendChild(el("td", null, u.manager || u.source));
      tbody.appendChild(tr);
    });

    var more = $("show-more");
    var remaining = state.filtered.length - slice.length;
    more.hidden = remaining <= 0;
    more.textContent = "Show " + Math.min(remaining, PAGE_SIZE) + " more (" + remaining + " remaining)";

    $("result-count").textContent =
      state.filtered.length.toLocaleString("en-US") + " of " +
      state.all.length.toLocaleString("en-US") + " units match";
  }

  // ── summary tiles ──────────────────────────────────────────────────────

  function renderStats() {
    var units = state.filtered;
    $("stat-units").textContent = units.length.toLocaleString("en-US");

    // Median rent is reported over whole units only, for the same reason the
    // rent chart is: per-room co-living prices would drag it down and make
    // the figure unrecognizable to someone shopping for an apartment.
    var rents = units
      .filter(function (u) { return isWholeUnit(u) && u.rent !== null && u.rent !== undefined; })
      .map(function (u) { return u.rent; });
    var med = median(rents);
    $("stat-median").textContent = med === null ? "—" : money(med);

    var cities = {};
    units.forEach(function (u) { if (u.city) cities[u.city] = 1; });
    $("stat-cities").textContent = Object.keys(cities).length;

    var mgrs = {};
    units.forEach(function (u) { if (u.manager) mgrs[u.manager] = 1; });
    $("stat-sources").textContent = Object.keys(mgrs).length;
  }

  // ── provenance ─────────────────────────────────────────────────────────

  function renderSources() {
    var grid = $("sources-grid");
    grid.innerHTML = "";
    var srcs = (state.meta.sources || []).filter(function (s) { return s.unit_count > 0; });

    srcs.forEach(function (s) {
      var row = el("div", "src");
      if (s.site) {
        var a = el("a", "src__name", s.manager);
        a.href = s.site;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        row.appendChild(a);
      } else {
        row.appendChild(el("span", "src__name", s.manager));
      }
      var count = el("span", "src__count" + (s.stale ? " src__stale" : ""),
        s.unit_count + (s.stale ? " · stale" : ""));
      if (s.stale) count.title = "Last successful scrape is more than " +
        (state.meta.stale_after_hours || 48) + " hours old";
      row.appendChild(count);
      grid.appendChild(row);
    });

    $("sources-sub").textContent =
      srcs.length + " managers contributing units. Counts are units after " +
      "de-duplication and after removing parking, storage and application listings.";
  }

  function renderGaps(gaps) {
    if (!gaps || !gaps.length) { $("gaps").hidden = true; return; }
    var body = $("gaps-body");
    body.innerHTML = "";
    var ul = el("ul");
    gaps.forEach(function (g) {
      var li = el("li");
      li.appendChild(el("strong", null, g.manager + " — "));
      li.appendChild(document.createTextNode(g.note || "No public unit data."));
      ul.appendChild(li);
    });
    body.appendChild(ul);
  }

  // ── filter controls ────────────────────────────────────────────────────

  function populateControls() {
    var facets = state.meta.facets || {};

    var citySel = $("f-city");
    (facets.cities || []).forEach(function (c) {
      var o = el("option", null, c.name + " (" + c.count + ")");
      o.value = c.name;
      citySel.appendChild(o);
    });

    var bedSel = $("f-beds");
    var seenFourPlus = false;
    (facets.bedrooms || []).forEach(function (b) {
      if (b.value === null) return;
      if (b.value >= 4) { seenFourPlus = true; return; }
      var o = el("option", null, b.label + " (" + b.count + ")");
      o.value = String(b.value);
      bedSel.appendChild(o);
    });
    if (seenFourPlus) {
      var o4 = el("option", null, "4+ bed");
      o4.value = "4plus";
      bedSel.appendChild(o4);
    }

    var mgrSel = $("f-manager");
    (state.meta.sources || [])
      .filter(function (s) { return s.unit_count > 0; })
      .slice()
      .sort(function (a, b) { return a.manager.localeCompare(b.manager); })
      .forEach(function (s) {
        var o = el("option", null, s.manager + " (" + s.unit_count + ")");
        o.value = s.manager;
        mgrSel.appendChild(o);
      });

    var rentMax = Math.ceil((((facets.rent || {}).max) || 10000) / 500) * 500;
    var rent = $("f-rent");
    rent.min = 0;
    rent.max = rentMax;
    rent.value = rentMax;
    $("f-rent-out").textContent = "Any";
  }

  /* Populate the amenity selects from merge.py's facet counts. Options are
     labelled with their count and the "unknown" bucket is skipped -- it is a
     real state (the source was silent) but not something you can filter
     *for*, and offering it would imply the dashboard knows more than it
     does. The count is shown so an empty-looking filter is obviously empty
     rather than broken. */
  function populateAmenityFilters(facets) {
    var SELECTS = {
      "f-roomtype": { facet: "room_type", labels: {
        entire_place: "Entire place", private_room: "Private room",
        shared_room: "Shared room" } },
      "f-pets": { facet: "pets", labels: AMENITY_LABELS.pets },
      "f-furnished": { facet: "furnished", labels: AMENITY_LABELS.furnished },
      "f-parking": { facet: "parking", labels: AMENITY_LABELS.parking },
      "f-laundry": { facet: "laundry", labels: AMENITY_LABELS.laundry }
    };
    Object.keys(SELECTS).forEach(function (id) {
      var node = $(id);
      if (!node) return;
      var spec = SELECTS[id];
      (facets[spec.facet] || []).forEach(function (row) {
        if (row.name === "unknown" || !spec.labels[row.name]) return;
        var opt = document.createElement("option");
        opt.value = row.name;
        opt.textContent = spec.labels[row.name] + " (" + row.count + ")";
        node.appendChild(opt);
      });
      node.disabled = node.options.length <= 1;
    });
  }

  function wireControls() {
    function rerender() {
      applyFilters();
      renderStats();
      renderCharts();
      renderTable();
    }

    $("f-city").addEventListener("change", function (e) {
      state.filters.city = e.target.value; rerender();
    });
    $("f-beds").addEventListener("change", function (e) {
      state.filters.beds = e.target.value; rerender();
    });
    $("f-manager").addEventListener("change", function (e) {
      state.filters.manager = e.target.value; rerender();
    });
    $("f-date").addEventListener("change", function (e) {
      state.filters.byDate = e.target.value; rerender();
    });
    $("f-whole").addEventListener("change", function (e) {
      state.filters.wholeOnly = e.target.checked; rerender();
    });
    $("f-sort").addEventListener("change", function (e) {
      state.filters.sort = e.target.value; sortFiltered(); renderTable();
    });

    // Amenity selects are populated from the facet counts merge.py computes,
    // so an option never appears unless something actually matches it.
    ["roomType", "pets", "furnished", "parking", "laundry"].forEach(function (key) {
      var id = "f-" + key.toLowerCase();
      var node = $(id);
      if (!node) return;
      node.addEventListener("change", function (e) {
        state.filters[key] = e.target.value; rerender();
      });
    });

    var rent = $("f-rent");
    rent.addEventListener("input", function (e) {
      var v = Number(e.target.value);
      var atMax = v >= Number(e.target.max);
      state.filters.maxRent = atMax ? null : v;
      $("f-rent-out").textContent = atMax ? "Any" : money(v);
      rerender();
    });

    $("f-reset").addEventListener("click", function () {
      state.filters = {
        city: "", beds: "", manager: "", maxRent: null,
        byDate: "", wholeOnly: false, roomType: "", pets: "",
        furnished: "", parking: "", laundry: "", sort: state.filters.sort
      };
      $("f-city").value = ""; $("f-beds").value = ""; $("f-manager").value = "";
      $("f-date").value = ""; $("f-whole").checked = false;
      ["roomtype", "pets", "furnished", "parking", "laundry"].forEach(function (id) {
        var n = $("f-" + id); if (n) n.value = "";
      });
      rent.value = rent.max; $("f-rent-out").textContent = "Any";
      rerender();
    });

    $("show-more").addEventListener("click", function () {
      state.shown += PAGE_SIZE;
      renderTable();
    });

    $("theme-toggle").addEventListener("click", function () {
      var root = document.documentElement;
      var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      var current = root.getAttribute("data-theme") || (prefersDark ? "dark" : "light");
      root.setAttribute("data-theme", current === "dark" ? "light" : "dark");
    });
  }

  // ── boot ───────────────────────────────────────────────────────────────

  function subtitle(meta) {
    var when = meta.generated_at ? new Date(meta.generated_at) : null;
    var stamp = when
      ? when.toLocaleString("en-US", {
          month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
        })
      : "unknown";
    var srcCount = (meta.sources || []).filter(function (s) { return s.unit_count > 0; }).length;
    return meta.total_units.toLocaleString("en-US") + " available units from " +
      srcCount + " property managers · updated " + stamp;
  }

  fetch(DATA_URL, { cache: "no-store" })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status + " fetching merged.json");
      return r.json();
    })
    .then(function (data) {
      state.meta = data;
      state.all = data.units || [];
      state.managers = data.managers || {};
      populateAmenityFilters(data.facets || {});

      $("subtitle").textContent = subtitle(data);
      populateControls();
      wireControls();
      applyFilters();
      renderStats();
      renderCharts();
      renderTable();
      renderSources();

      // The unscrapable list is served alongside the data by run_all_scrapers.
      fetch("../data/raw/_status.json", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) { renderGaps(s && s.unscrapable); })
        .catch(function () { /* provenance detail is optional */ });

      $("loading").hidden = true;
      $("page").hidden = false;
    })
    .catch(function (err) {
      $("loading").hidden = true;
      var e = $("error");
      e.hidden = false;
      e.textContent = "Could not load listings data: " + err.message +
        ". If you are running locally, serve the repo root over HTTP " +
        "(python scripts/serve.py) rather than opening the file directly.";
    });
})();
