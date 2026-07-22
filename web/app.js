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
    filtered: [],
    shown: PAGE_SIZE,
    filters: {
      city: "",
      beds: "",
      manager: "",
      maxRent: null,
      byDate: "",
      wholeOnly: false,
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
     apartment. Rooms get their own label and cite the floor plan instead. */
  function bedLabel(u) {
    var v = u.bedrooms;
    if (u.kind === "room") return v ? "Room in " + v + " bd" : "Room";
    if (v === null || v === undefined) return "—";
    if (v === 0) return "Studio";
    return v + " bd";
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
      if (f.wholeOnly && u.kind !== "residential") return false;

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
      if (u.kind !== "residential") return;
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
      if (u.kind === "room") {
        wrap.appendChild(el("span", "addr__note", u.notes || "Private room in a shared unit"));
      } else if (u.notes) {
        wrap.appendChild(el("span", "addr__note", u.notes));
      }
      tdAddr.appendChild(wrap);
      tr.appendChild(tdAddr);

      tr.appendChild(el("td", null, u.city || "—"));

      var tdBeds = el("td", "num");
      tdBeds.appendChild(el("span", u.bedrooms === null && u.kind !== "room" ? "muted" : null, bedLabel(u)));
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
      .filter(function (u) { return u.kind === "residential" && u.rent !== null && u.rent !== undefined; })
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
        byDate: "", wholeOnly: false, sort: state.filters.sort
      };
      $("f-city").value = ""; $("f-beds").value = ""; $("f-manager").value = "";
      $("f-date").value = ""; $("f-whole").checked = false;
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
