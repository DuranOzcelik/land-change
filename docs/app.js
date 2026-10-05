/* Land Change front end: globe, place search, API calls and result rendering. */
(() => {
  "use strict";

  const API = window.LAND_CHANGE_API;
  const LANGS = window.LANGS;
  const GROUPS = ["agriculture", "vegetation", "built", "water"];
  // EuroSAT class order as in /api/meta; grid.classes holds indexes into this list.
  const CLASSES = ["AnnualCrop", "Forest", "HerbaceousVegetation", "Highway", "Industrial",
    "Pasture", "PermanentCrop", "Residential", "River", "SeaLake"];
  // Legend order: classes listed group by group, as /api/meta groups them.
  const CLASS_ORDER = ["AnnualCrop", "PermanentCrop", "Pasture", "Forest", "HerbaceousVegetation",
    "Residential", "Industrial", "Highway", "River", "SeaLake"];
  // Share differences below this many percentage points are read as noise, not change.
  const NOISE_PTS = 1;
  const PHOTON = "https://photon.komoot.io";
  const BASEMAP = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg";

  const $ = (id) => document.getElementById(id);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };

  const state = {
    lang: store.get("lang") || ((navigator.language || "en").toLowerCase().startsWith("tr") ? "tr" : "en"),
    theme: store.get("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
    place: null,          // { lat, lon, label }
    mode: "now",          // "now" | "change" | "time"
    cell: 640,
    data: null,           // last API response
    layer: "split",       // what the map shows in change mode: split | before | after | change
    split: 0.5,           // handle position as a share of the map width
    color: "groups",      // cell colouring: groups | classes | confidence
    prior: store.get("prior") !== "off",   // apply the Natural Earth correction
    opacity: 0.55,
  };

  /* ------------------------------------------------------------------ language and theme */

  const t = (key) => (LANGS[state.lang] && LANGS[state.lang][key]) ?? LANGS.en[key] ?? key;
  const num = (value, digits = 1) =>
    new Intl.NumberFormat(state.lang, { maximumFractionDigits: digits }).format(value);
  const pct = (value) => (state.lang === "tr" ? `%${num(value)}` : `${num(value)}%`);

  function applyLanguage() {
    document.documentElement.lang = state.lang;
    document.title = t("title");
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nLabel)); });
    renderSections();
    $("run").textContent = t(RUN_LABEL[state.mode]);
    $("date-help").textContent = t(state.mode === "time" ? "timeHelp" : "dateHelp");
    $("theme").textContent = `${t("theme")}: ${t(state.theme)}`;
    $("split-handle").setAttribute("aria-label", t("slider"));
    if (stageKey) setStage(stageKey);
    if (state.data && state.data.story) {
      state.data.label = storyTitle(state.data.story);
      if (state.place) state.place.label = state.data.label;
    }
    showPlaceLine();
    if (state.data) renderResults();
  }

  function applyTheme() {
    document.documentElement.dataset.theme = state.theme;
    $("theme").textContent = `${t("theme")}: ${t(state.theme)}`;
  }

  /* ------------------------------------------------------------------ globe */

  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const wide = () => innerWidth > 860;
  const baseStyle = () => ({
    version: 8,
    projection: { type: "globe" },
    sources: { sat: { type: "raster", tiles: [BASEMAP], tileSize: 256, maxzoom: 14 } },
    layers: [
      { id: "ocean", type: "background", paint: { "background-color": "#0b2942" } },
      { id: "sat", type: "raster", source: "sat" },
    ],
    sky: { "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 5, 1, 7, 0] },
  });
  const map = new maplibregl.Map({
    container: "map",
    style: baseStyle(),
    center: [32, 38],
    zoom: wide() ? 2.1 : 0.9,
    attributionControl: false,
    dragRotate: false,
  });
  map.touchZoomRotate.disableRotation();

  const padding = () => {
    if (!wide()) return { top: 24, bottom: 24, left: 24, right: 24 };
    const roomy = innerWidth >= 1280;
    return { top: 90, bottom: 50, left: 440, right: roomy && !$("results").hidden ? $("results").offsetWidth + 40 : 40 };
  };

  // The globe turns slowly until the visitor touches it.
  let spinning = !reducedMotion;
  function spin() {
    if (!spinning) return;
    const c = map.getCenter();
    map.jumpTo({ center: [c.lng + 0.06, c.lat] });
    requestAnimationFrame(spin);
  }
  const stopSpin = () => { spinning = false; };
  ["mousedown", "touchstart", "wheel"].forEach((ev) => map.getCanvas().addEventListener(ev, stopSpin, { passive: true }));

  function addFrame(target) {
    target.addSource("frame", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    target.addLayer({ id: "frame", type: "line", source: "frame", paint: { "line-color": "#ffffff", "line-width": 2 } });
  }

  // Results may arrive before the style has loaded (saved examples open instantly).
  let mapLoaded;
  const mapReady = new Promise((resolve) => { mapLoaded = resolve; });

  map.on("load", () => {
    addFrame(map);
    mapLoaded();
    restoreFromLink();
    spin();
  });

  map.on("click", (e) => {
    const { lat, lng } = e.lngLat;
    const coords = `${lat.toFixed(3)}, ${lng.toFixed(3)}`;
    setPlace(lat, lng, coords);
    nameAt(lat, lng).then((name) => {
      if (name && state.place && state.place.lat === lat) { state.place.label = name; showPlaceLine(); }
    });
  });

  // A readable name for a point: the sea or lake when it is open water, otherwise the nearest
  // place from Photon, otherwise a Natural Earth area (desert, mountains, island...). Areas that
  // span hundreds of kilometres keep the coordinates next to the name.
  async function nameAt(lat, lon) {
    const coords = `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
    // Only settlement-level results: a bare reverse lookup tends to return the nearest shop.
    const layers = "&layer=locality&layer=district&layer=city&layer=county";
    const photon = fetch(`${PHOTON}/reverse?lat=${lat}&lon=${lon}${layers}${state.lang === "en" ? "&lang=en" : ""}`)
      .then((r) => r.json()).then((j) => (j.features && j.features[0]) || null).catch(() => null);
    const [water, area, place] = await Promise.all([inOpenWater(lat, lon), geoName(lat, lon), photon]);
    const km = (f) => {
      const [plon, plat] = f.geometry.coordinates;
      return Math.hypot((plon - lon) * 111.32 * Math.cos((lat * Math.PI) / 180), (plat - lat) * 110.574);
    };
    if (area && (water || !place || km(place) > 10)) return `${area.name}, ${coords}`;
    return place ? placeLabel(place).full : null;
  }

  /* ------------------------------------------------------------------ split view */

  // MapLibre layers cannot be clipped, so the later date lives in a second, camera-locked
  // map stacked on the first one and clipped to the right of the handle.
  let afterMap = null;
  let afterReady = null;
  const maps = () => (afterMap ? [map, afterMap] : [map]);

  function syncCamera() {
    if (!afterMap || $("map-after").hidden) return;
    afterMap.jumpTo({
      center: map.getCenter(), zoom: map.getZoom(), bearing: map.getBearing(),
      pitch: map.getPitch(), padding: map.getPadding(),
    });
  }
  map.on("move", syncCamera);
  map.on("resize", syncCamera);

  function ensureAfterMap() {
    if (afterReady) return afterReady;
    afterMap = new maplibregl.Map({
      container: "map-after", style: baseStyle(), interactive: false, attributionControl: false,
      center: map.getCenter(), zoom: map.getZoom(),
    });
    afterMap.on("resize", syncCamera);
    afterReady = new Promise((resolve) => afterMap.on("load", () => {
      addFrame(afterMap);
      if (frameCorners) setFrame(frameCorners);
      placeMarkers();
      syncCamera();
      resolve(afterMap);
    }));
    return afterReady;
  }

  const splitHandle = $("split-handle");
  const mapWrap = $("map-wrap");

  // The handle stays on the visible map: inside the screen and, on wide screens, between the panels.
  function splitBounds() {
    const w = Math.max(mapWrap.clientWidth, 88);
    let lo = 22, hi = w - 22;
    if (wide()) {
      lo = Math.max(lo, $("controls").getBoundingClientRect().right + 22);
      if (!$("results").hidden) hi = Math.min(hi, $("results").getBoundingClientRect().left - 22);
      if (hi <= lo) { lo = 22; hi = w - 22; }
    }
    return [lo / w, hi / w];
  }

  function moveSplit(share) {
    const [lo, hi] = splitBounds();
    state.split = Math.min(hi, Math.max(lo, share));
    const pct = state.split * 100;
    $("map-after").style.clipPath = `inset(0 0 0 ${pct}%)`;
    splitHandle.style.left = `${pct}%`;
    splitHandle.setAttribute("aria-valuenow", String(Math.round(pct)));
    splitHandle.setAttribute("aria-valuetext", t("splitValue").replace("{n}", num(100 - pct, 0)));
  }

  function setSplit(on) {
    splitHandle.hidden = !on;
    $("map-after").hidden = !on;
    if (!on) return;
    if (afterMap) { afterMap.resize(); syncCamera(); }
    moveSplit(state.split);
  }

  // Default handle position: the middle of the analysed square once the map has fitted it.
  function centreSplit() {
    const p = padding();
    const w = mapWrap.clientWidth;
    state.split = w > p.left + p.right ? (p.left + (w - p.left - p.right) / 2) / w : 0.5;
  }

  const shareAt = (clientX) => {
    const r = mapWrap.getBoundingClientRect();
    return (clientX - r.left) / r.width;
  };
  splitHandle.addEventListener("pointerdown", (e) => {
    splitHandle.setPointerCapture(e.pointerId);
    splitHandle.classList.add("dragging");
    moveSplit(shareAt(e.clientX));
  });
  splitHandle.addEventListener("pointermove", (e) => {
    if (splitHandle.hasPointerCapture(e.pointerId)) moveSplit(shareAt(e.clientX));
  });
  ["pointerup", "pointercancel"].forEach((ev) =>
    splitHandle.addEventListener(ev, () => splitHandle.classList.remove("dragging")));
  addEventListener("resize", () => { if (!splitHandle.hidden) moveSplit(state.split); });
  splitHandle.addEventListener("keydown", (e) => {
    const step = e.shiftKey || e.key.startsWith("Page") ? 0.1 : 0.02;
    const next = {
      ArrowLeft: state.split - step, ArrowDown: state.split - step, PageDown: state.split - step,
      ArrowRight: state.split + step, ArrowUp: state.split + step, PageUp: state.split + step,
      Home: 0, End: 1,
    }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    moveSplit(next);
  });

  /* ------------------------------------------------------------------ map overlays */

  const markers = new Map();
  let frameCorners = null;

  function placeMarkers() {
    if (!state.place) return;
    maps().forEach((target) => {
      if (!markers.has(target)) {
        const el = document.createElement("div");
        el.className = "pin";
        markers.set(target, new maplibregl.Marker({ element: el }).setLngLat([state.place.lon, state.place.lat]).addTo(target));
      }
      markers.get(target).setLngLat([state.place.lon, state.place.lat]);
    });
  }

  function approxSquare(lat, lon, km) {
    const dLat = km / 2 / 110.574;
    const dLon = km / 2 / (111.32 * Math.cos((lat * Math.PI) / 180));
    return [[lon - dLon, lat + dLat], [lon + dLon, lat + dLat], [lon + dLon, lat - dLat], [lon - dLon, lat - dLat]];
  }

  function setFrame(corners) {
    frameCorners = corners;
    maps().forEach((target) => {
      const src = target.getSource("frame");
      if (src) src.setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [...corners, corners[0]] } });
    });
  }

  function fit(corners, duration = 1400) {
    const lons = corners.map((c) => c[0]);
    const lats = corners.map((c) => c[1]);
    map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: padding(), duration: reducedMotion ? 0 : duration });
  }

  function setImage(target, id, url, coordinates, paint) {
    const src = target.getSource(id);
    if (src) { src.updateImage({ url, coordinates }); return; }
    target.addSource(id, { type: "image", url, coordinates });
    target.addLayer({ id, type: "raster", source: id, paint }, "frame");
  }

  /* ------------------------------------------------------------------ place selection */

  function setPlace(lat, lon, label, fly = true) {
    stopSpin();
    lat = Math.max(-60, Math.min(75, lat));
    state.place = { lat, lon, label };
    $("hero").hidden = true;
    $("options").hidden = false;
    placeMarkers();
    const square = approxSquare(lat, lon, Number($("size").value));
    setFrame(square);
    if (fly) fit(square);
    showPlaceLine();
    setStatus("");
  }

  function showPlaceLine() {
    const line = $("place-line");
    line.classList.toggle("place", Boolean(state.place));
    line.textContent = state.place ? `${t("selected")}: ${state.place.label}` : t("searchHint");
  }

  function placeLabel(feature) {
    const p = feature.properties || {};
    const rest = [...new Set([p.district, p.city, p.county, p.state, p.country].filter((x) => x && x !== p.name))];
    const name = p.name || rest.shift() || "";
    return { name, rest: rest.join(", "), full: [name, ...rest.slice(0, 2)].filter(Boolean).join(", ") };
  }

  /* ------------------------------------------------------------------ search with suggestions */

  const searchInput = $("search");
  const list = $("suggestions");
  let suggestTimer = null;
  let suggestAbort = null;
  let options = [];
  let active = -1;

  function closeSuggestions() { list.hidden = true; list.innerHTML = ""; options = []; active = -1; }

  function showSuggestions(items) {
    options = items;
    active = -1;
    list.innerHTML = items.length
      ? items.map((o, i) => `<li role="option" data-i="${i}" aria-selected="false">${esc(o.name)}<small>${esc(o.rest)}</small></li>`).join("")
      : `<li class="empty">${esc(t("noResults"))}</li>`;
    list.hidden = false;
  }

  function choose(i) {
    const o = options[i];
    if (!o) return;
    searchInput.value = o.full;
    closeSuggestions();
    setPlace(o.lat, o.lon, o.full);
  }

  async function suggest(query) {
    const coords = query.match(/^\s*(-?\d+(?:\.\d+)?)\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*$/);
    if (coords) {
      const lat = Number(coords[1]), lon = Number(coords[2]);
      const label = `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
      showSuggestions([{ name: label, rest: "", full: label, lat, lon }]);
      const name = await nameAt(lat, lon);
      if (name && searchInput.value.trim() === query) {
        const title = name.endsWith(label) ? name.slice(0, -label.length - 2) : name;
        showSuggestions([{ name: title, rest: label, full: name, lat, lon }]);
      }
      return;
    }
    if (suggestAbort) suggestAbort.abort();
    suggestAbort = new AbortController();
    try {
      const lang = state.lang === "en" ? "&lang=en" : "";
      const res = await fetch(`${PHOTON}/api/?q=${encodeURIComponent(query)}&limit=6${lang}`, { signal: suggestAbort.signal });
      const json = await res.json();
      showSuggestions((json.features || []).map((f) => {
        const [lon, lat] = f.geometry.coordinates;
        return { ...placeLabel(f), lat, lon };
      }));
    } catch (err) {
      if (err.name !== "AbortError") closeSuggestions();
    }
  }

  searchInput.addEventListener("input", () => {
    clearTimeout(suggestTimer);
    const q = searchInput.value.trim();
    if (q.length < 3) return closeSuggestions();
    suggestTimer = setTimeout(() => suggest(q), 250);
  });
  searchInput.addEventListener("keydown", (e) => {
    if (list.hidden || !options.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      [...list.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === active)));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(active >= 0 ? active : 0);
    } else if (e.key === "Escape") {
      closeSuggestions();
    }
  });
  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) choose(Number(li.dataset.i));
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) closeSuggestions(); });

  /* ------------------------------------------------------------------ form */

  const RUN_LABEL = { now: "runNow", change: "runChange", time: "runTime" };

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("#mode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
    $("fields-now").hidden = mode !== "now";
    $("fields-change").hidden = mode !== "change";
    $("fields-time").hidden = mode !== "time";
    $("date-help").textContent = t(mode === "time" ? "timeHelp" : "dateHelp");
    closeDates();
    $("run").textContent = t(RUN_LABEL[mode]);
  }
  $("mode").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); });

  function setCell(cell) {
    state.cell = cell;
    document.querySelectorAll("#cell button").forEach((b) => b.setAttribute("aria-selected", String(Number(b.dataset.cell) === cell)));
  }
  $("cell").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setCell(Number(b.dataset.cell)); });

  $("size").addEventListener("change", () => {
    if (state.place) setFrame(approxSquare(state.place.lat, state.place.lon, Number($("size").value)));
  });

  // Default "after" year: the last summer that has already happened.
  const today = new Date();
  const lastSummer = today.getMonth() >= 7 ? today.getFullYear() : today.getFullYear() - 1;
  $("after").value = String(lastSummer);
  $("end").value = String(lastSummer);

  /* ------------------------------------------------------------------ available dates */

  const datesCache = new Map();
  let datesAbort = null;

  function closeDates() {
    if (datesAbort) datesAbort.abort();
    document.querySelectorAll(".date-list").forEach((l) => { l.hidden = true; l.innerHTML = ""; });
    document.querySelectorAll("[data-dates]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }

  async function fetchDates(year) {
    const key = `${state.place.lat.toFixed(4)},${state.place.lon.toFixed(4)},${year}`;
    if (datesCache.has(key)) return datesCache.get(key);
    datesAbort = new AbortController();
    const params = new URLSearchParams({ lat: state.place.lat.toFixed(5), lon: state.place.lon.toFixed(5), year });
    const res = await fetch(`${API}/api/dates?${params}`, { signal: datesAbort.signal });
    if (!res.ok) throw new Error(t("errNetwork"));
    const { dates } = await res.json();
    datesCache.set(key, dates);
    return dates;
  }

  async function openDates(button) {
    const id = button.dataset.dates;
    const input = $(id), list = $(`dates-${id}`);
    const wasOpen = !list.hidden;
    closeDates();
    if (wasOpen) return;
    if (!state.place) return setStatus(t("pickFirst"), true);

    const show = (html) => { list.innerHTML = html; list.hidden = false; button.setAttribute("aria-expanded", "true"); };
    const typed = input.value.trim().match(/^(\d{4})/);
    const year = typed ? Number(typed[1]) : lastSummer;
    if (year < 2015 || year > today.getFullYear()) {
      return show(`<p class="empty">${esc(t("datesYear").replace("{max}", today.getFullYear()))}</p>`);
    }
    show(`<p class="empty">${esc(t("datesLoading"))}</p>`);
    try {
      const dates = await fetchDates(year);
      if (list.hidden) return;
      if (!dates.length) return show(`<p class="empty">${esc(t("datesEmpty"))}</p>`);
      show(`<ul aria-label="${esc(t("datesList").replace("{year}", year))}">${dates.map((d) => `
        <li><button type="button" data-date="${esc(d.date)}" aria-current="${d.date === input.value.trim()}">
          <span>${esc(d.date)}</span><small>${esc(t("cloud").replace("{n}", num(d.cloud)))}</small></button></li>`).join("")}</ul>`);
      (list.querySelector('[aria-current="true"]') || list.querySelector("button")).focus();
    } catch (err) {
      if (err.name !== "AbortError") show(`<p class="empty error">${esc(err instanceof TypeError ? t("errNetwork") : err.message)}</p>`);
    }
  }

  document.querySelectorAll("[data-dates]").forEach((b) => b.addEventListener("click", () => openDates(b)));
  document.querySelectorAll(".date-list").forEach((list) => {
    const input = $(list.id.replace("dates-", ""));
    list.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-date]");
      if (!b) return;
      input.value = b.dataset.date;
      closeDates();
      input.focus();
    });
    list.addEventListener("keydown", (e) => {
      const buttons = [...list.querySelectorAll("button")];
      const i = buttons.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        buttons[Math.min(buttons.length - 1, Math.max(0, i + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
      } else if (e.key === "Escape") {
        closeDates();
        document.querySelector(`[data-dates="${input.id}"]`).focus();
      }
    });
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".field")) closeDates(); });

  let runAbort = null;    // aborts the running analysis request
  let stageKey = null;    // translation key of the loading stage on screen, so a language switch can redraw it

  function setStatus(message, isError = false) {
    const el = $("status");
    el.hidden = !message;
    el.textContent = message;
    el.classList.toggle("error", isError);
    stageKey = null;
    $("status-row").hidden = el.hidden && $("cancel").hidden;
  }

  function setStage(key) {
    setStatus(t(key));
    stageKey = key;
  }

  const validDate = (v) => /^\d{4}(-\d{2}-\d{2})?$/.test(v);

  $("controls").addEventListener("submit", async (e) => {
    e.preventDefault();
    stopPlaying();
    if (!state.place) return setStatus(t("pickFirst"), true);

    const params = new URLSearchParams({
      lat: state.place.lat.toFixed(5), lon: state.place.lon.toFixed(5),
      size_km: $("size").value, cell_m: String(state.cell),
    });
    let path = "/api/analyze";
    if (state.mode === "time") {
      const start = Number($("start").value.trim()), end = Number($("end").value.trim());
      const ok = /^\d{4}$/.test($("start").value.trim()) && /^\d{4}$/.test($("end").value.trim())
        && start >= 2016 && end <= today.getFullYear() && start < end && end - start + 1 <= 13;
      if (!ok) return setStatus(t("errYears").replace("{max}", today.getFullYear()), true);
      return runTimeline(params, start, end);
    }
    if (state.mode === "now") {
      const when = $("when").value.trim();
      if (when && !validDate(when)) return setStatus(t("errDate"), true);
      if (when) params.set("when", when);
    } else {
      const before = $("before").value.trim(), after = $("after").value.trim();
      if (!validDate(before) || !validDate(after) || before >= after) return setStatus(t("errDate"), true);
      params.set("before", before);
      params.set("after", after);
      path = "/api/compare";
    }

    // The server reports no progress, so the stages follow typical timings and are worded as estimates.
    const button = $("run"), cancel = $("cancel");
    button.disabled = true;
    cancel.hidden = false;
    runAbort = new AbortController();
    setStage("stageSearch");
    let cancelled = false;
    const timers = [[3000, "stageDownload"], [8000, "stageClassify"], [12000, "waking"]]
      .map(([ms, key]) => setTimeout(() => setStage(key), ms));
    try {
      const res = await fetch(`${API}${path}?${params}`, { signal: runAbort.signal });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || "");
        throw new Error(res.status === 404 ? t("errNoImagery") : res.status === 422 ? t("errInput") + detail : t("errNetwork"));
      }
      const raw = await res.json();
      const { body, prior } = await withPrior(state.mode, raw);
      await mapReady;
      state.data = { mode: state.mode, raw, body, prior, label: state.place.label };
      state.layer = "split";
      sheetOpen = false;
      setStatus("");
      $("results").hidden = false;
      centreSplit();
      renderResults();
      updateLink(params);
    } catch (err) {
      cancelled = err.name === "AbortError";
      if (cancelled) setStatus(t("cancelled"));
      else setStatus(err instanceof TypeError ? t("errNetwork") : err.message, true);
    } finally {
      timers.forEach(clearTimeout);
      runAbort = null;
      cancel.hidden = true;
      $("status-row").hidden = $("status").hidden;
      button.disabled = false;
      // The cancel button disappears, so hand focus back to the run button.
      if (cancelled) button.focus();
    }
  });

  $("cancel").addEventListener("click", () => { if (runAbort) runAbort.abort(); });

  /* ------------------------------------------------------------------ over-time mode */

  // One /api/analyze per year, two at a time (the server runs at most two heavy jobs), so the
  // chart fills in as years arrive and every year brings its own image for the animation.
  const YEAR_CONCURRENCY = 2;

  async function runTimeline(params, start, end) {
    const years = [];
    for (let y = start; y <= end; y++) years.push(y);
    // points[year]: a layer, null for a gap (no image or failed), undefined while pending.
    const series = { years, points: {}, raw: {}, priors: {}, missing: [], failed: [] };
    stopPlaying();
    state.data = { mode: "time", body: series, label: state.place.label };
    state.timeYear = null;
    sheetOpen = false;

    const button = $("run"), cancel = $("cancel");
    button.disabled = true;
    cancel.hidden = false;
    runAbort = new AbortController();
    const { signal } = runAbort;
    const started = Date.now();
    let done = 0;
    const progress = () => setStatus(t(!done && Date.now() - started >= 12000 ? "yearsWaking" : "yearsProgress")
      .replace("{done}", done).replace("{total}", years.length));
    progress();
    const wake = setTimeout(progress, 12000);

    $("results").hidden = false;
    renderResults();
    const link = new URLSearchParams(params);
    link.set("start", start);
    link.set("end", end);
    updateLink(link);

    const queue = [...years];
    async function worker() {
      while (queue.length && !signal.aborted) {
        const year = queue.shift();
        const query = new URLSearchParams(params);
        query.set("when", String(year));
        try {
          const res = await fetch(`${API}/api/analyze?${query}`, { signal });
          if (res.ok) {
            series.raw[year] = await res.json();
            const { body, prior } = await withPrior("time", series.raw[year]);
            series.points[year] = body;
            series.priors[year] = prior;
          }
          else {
            series.points[year] = null;
            (res.status === 404 ? series.missing : series.failed).push(year);
          }
        } catch (err) {
          if (err.name === "AbortError") return;
          series.points[year] = null;
          series.failed.push(year);
        }
        done++;
        await mapReady;
        if (state.data && state.data.body === series) {
          progress();
          updateTimeline();
        }
      }
    }

    try {
      await Promise.all(Array.from({ length: YEAR_CONCURRENCY }, worker));
    } finally {
      clearTimeout(wake);
      runAbort = null;
      cancel.hidden = true;
      button.disabled = false;
      const loaded = years.filter((y) => series.points[y]).length;
      if (signal.aborted) {
        setStatus(t("cancelled"));
        button.focus();
      } else if (!loaded) {
        setStatus(series.failed.length ? t("errNetwork") : t("errNoImagery"), true);
      } else {
        setStatus("");
      }
      if (state.data && state.data.body === series) {
        updateTimeline();
        if (!signal.aborted && !reducedMotion && loadedYears(series).length > 1) play(true);
      }
    }
  }

  const loadedYears = (body) => body.years.filter((y) => body.points[y]);
  const groupShare = (layer, key) => layer.stats.groups.find((g) => g.key === key).share;
  const fadedYear = (layer) => layer.unusable_share > 5;

  // Redraws the parts of the time panel that change as years arrive or the selection moves.
  function updateTimeline() {
    const body = state.data.body;
    const loaded = loadedYears(body);
    if (!loaded.includes(state.timeYear)) state.timeYear = loaded[0] ?? null;
    const focused = document.activeElement && document.activeElement.id;
    $("time-figure").innerHTML = timeFigure(body);
    $("timeline").innerHTML = timelineHtml(body);
    $("time-color").innerHTML = state.timeYear ? colorControl(body.points[state.timeYear]) : "";
    if (focused === "chart") $("chart").focus();
    syncPlayer();
    if (state.timeYear !== state.data.paintedYear) {
      state.data.paintedYear = state.timeYear;
      const side = showOnMap();
      if (side && !state.data.fitted) {
        fit(side.image.corners, 900);
        state.data.fitted = true;
      }
    }
    applySheet();
  }

  function selectYear(year, announce) {
    if (year === state.timeYear) return;
    state.timeYear = year;
    updateTimeline();
    if (announce) $("chart-live").textContent = yearSummary(year);
  }

  /* ------------------------------------------------------------------ time-lapse */

  // Plays the loaded years on the map. The controls sit in the panel head, which stays in view
  // (also in the closed phone sheet). With reduced motion there is no playback, only the slider.
  const SPEEDS = [0.5, 1, 2];
  const FRAME_MS = 1200;
  let playTimer = null;
  let speed = 1;

  function playerHtml() {
    const controls = reducedMotion ? "" : `
      <button class="pill-button small" id="play" type="button" aria-pressed="false">${esc(t("play"))}</button>`;
    const speeds = reducedMotion ? "" : `
      <label class="speed"><span>${esc(t("speed"))}</span>
        <select id="speed" class="pill-select">${SPEEDS.map((v) => `<option value="${v}"${v === speed ? " selected" : ""}>${num(v)}×</option>`).join("")}</select></label>`;
    return `<div class="player" id="player" hidden>${controls}
      <input id="year-slider" type="range" min="0" max="0" step="1" value="0" aria-label="${esc(t("year"))}">
      ${speeds}</div>`;
  }

  function syncPlayer() {
    const player = $("player");
    if (!player) return;
    const loaded = loadedYears(state.data.body);
    player.hidden = loaded.length < 2;
    const slider = $("year-slider");
    slider.max = String(Math.max(0, loaded.length - 1));
    slider.value = String(Math.max(0, loaded.indexOf(state.timeYear)));
    slider.setAttribute("aria-valuetext", String(state.timeYear ?? ""));
    const button = $("play");
    if (button) {
      button.textContent = t(playTimer ? "pause" : "play");
      button.setAttribute("aria-pressed", String(Boolean(playTimer)));
    }
  }

  function step() {
    const loaded = loadedYears(state.data.body);
    const i = loaded.indexOf(state.timeYear);
    selectYear(loaded[(i + 1) % loaded.length], false);
    playTimer = setTimeout(step, FRAME_MS / speed);
    syncPlayer();
  }

  function play(fromStart) {
    if (reducedMotion || !state.data || state.data.mode !== "time") return;
    stopPlaying();
    if (fromStart) selectYear(loadedYears(state.data.body)[0], false);
    playTimer = setTimeout(step, FRAME_MS / speed);
    syncPlayer();
  }

  function stopPlaying() {
    clearTimeout(playTimer);
    playTimer = null;
    if (state.data && state.data.mode === "time") syncPlayer();
  }

  $("results").addEventListener("click", (e) => {
    if (e.target.id === "play") (playTimer ? stopPlaying : play)(false);
    else if (e.target.closest("#chart")) stopPlaying();
  });
  $("results").addEventListener("input", (e) => {
    if (e.target.id !== "year-slider") return;
    const year = loadedYears(state.data.body)[Number(e.target.value)];   // read before stopPlaying resyncs the slider
    stopPlaying();
    selectYear(year, false);
  });
  $("results").addEventListener("change", (e) => {
    if (e.target.id !== "speed") return;
    speed = Number(e.target.value);
    if (playTimer) play(false);
  });
  $("results").addEventListener("keydown", (e) => { if (e.target.id === "chart") stopPlaying(); });

  function yearSummary(year) {
    const layer = state.data.body.points[year];
    if (!layer) return `${year}: ${t(state.data.body.failed.includes(year) ? "yearFailed" : "yearMissing")}`;
    return `${year} (${layer.date}): ${GROUPS.map((g) => `${t(g)} ${pct(groupShare(layer, g))}`).join(", ")}`;
  }

  /* ------------------------------------------------------------------ drawing the cell grid */

  function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }

  // Confidence (0-100) to opacity: sure cells fade out, doubtful ones stand out.
  const CONF_CLEAR = 95, CONF_FULL = 50;
  const confidenceAlpha = (conf) =>
    Math.round(255 * Math.min(1, Math.max(0, (CONF_CLEAR - conf) / (CONF_CLEAR - CONF_FULL))));

  // Returns (row, col) => [r, g, b, a] for the current colouring.
  function cellColour(side, changed) {
    if (changed) {
      const on = hexToRgb(cssVar("--c-changed"));
      return (r, c) => (changed[r][c] ? [...on, 255] : [0, 0, 0, 150]);
    }
    if (state.color === "confidence") {
      const ink = hexToRgb(cssVar("--c-lowconf"));
      return (r, c) => [...ink, side.grid.groups[r][c] < 0 ? 0 : confidenceAlpha(side.grid.confidence[r][c])];
    }
    if (state.color === "classes") {
      const colors = CLASSES.map((k) => hexToRgb(cssVar(`--c-${k}`)));
      return (r, c) => (side.grid.classes[r][c] < 0 ? [0, 0, 0, 0] : [...colors[side.grid.classes[r][c]], 255]);
    }
    const colors = GROUPS.map((g) => hexToRgb(cssVar(`--c-${g}`)));
    return (r, c) => (side.grid.groups[r][c] < 0 ? [0, 0, 0, 0] : [...colors[side.grid.groups[r][c]], 255]);
  }

  // One pixel per cell, then enlarged without smoothing so cells stay crisp on the map.
  function gridCanvas(side, changed) {
    const rows = side.grid.rows, cols = side.grid.cols;
    const small = document.createElement("canvas");
    small.width = cols; small.height = rows;
    const ctx = small.getContext("2d");
    const img = ctx.createImageData(cols, rows);
    const colour = cellColour(side, changed);
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        img.data.set(colour(r, c), (r * cols + c) * 4);
      }
    }
    ctx.putImageData(img, 0, 0);
    const scale = Math.max(1, Math.floor(1024 / Math.max(rows, cols)));
    const big = document.createElement("canvas");
    big.width = cols * scale; big.height = rows * scale;
    const bctx = big.getContext("2d");
    bctx.imageSmoothingEnabled = false;
    bctx.drawImage(small, 0, 0, big.width, big.height);
    return big;
  }

  /* ------------------------------------------------------------------ prior knowledge */

  // Natural Earth geography (docs/data/geo, built by scripts/build_geo_prior.py) supplies what the
  // model cannot know: cells whose centre lies in open water well away from any shore count as
  // water, and glacier cells are left out of the statistics (EuroSAT has no ice class). Deserts and
  // mountain ranges only get a warning, since real towns and fields exist there too.
  const PRIOR_WARN = ["desert", "mountain"];
  const NAME_ORDER = ["lake", "sea", "ice", "desert", "mountain", "island", "peninsula", "plateau", "tundra", "delta"];

  async function geoTile(lat, lon) {
    const index = await loadData("geo/index");
    const key = `${Math.floor(lat / index.tile)}_${Math.floor(lon / index.tile)}`;
    return index.tiles.includes(key) ? loadData(`geo/${key}`) : null;
  }

  // Ray casting over rings of flat [lon, lat, ...] arrays; holes flip the parity.
  function inRings(rings, x, y) {
    let inside = false;
    for (const r of rings) {
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
      }
    }
    return inside;
  }
  const inAny = (polys, x, y) => polys.some((rings) => inRings(rings, x, y));

  // Named area at a point (sea, lake, desert...), in the interface language, or null.
  async function geoName(lat, lon) {
    try {
      const tile = await geoTile(lat, lon);
      if (!tile) return null;
      const hits = tile.n.filter((f) => inAny(f.p, lon, lat));
      hits.sort((a, b) => NAME_ORDER.indexOf(a.k) - NAME_ORDER.indexOf(b.k));
      return hits.length ? { kind: hits[0].k, name: hits[0].name[state.lang] || hits[0].name.en } : null;
    } catch {
      return null;
    }
  }

  async function inOpenWater(lat, lon) {
    try {
      const tile = await geoTile(lat, lon);
      return Boolean(tile && inAny(tile.w, lon, lat));
    } catch {
      return false;
    }
  }

  function cellCentres(grid) {
    const [tl, tr, br, bl] = grid.corners;
    const out = [];
    for (let r = 0; r < grid.rows; r++) {
      for (let c = 0; c < grid.cols; c++) {
        const u = (c + 0.5) / grid.cols, v = (r + 0.5) / grid.rows;
        const top = [tl[0] + (tr[0] - tl[0]) * u, tl[1] + (tr[1] - tl[1]) * u];
        const bottom = [bl[0] + (br[0] - bl[0]) * u, bl[1] + (br[1] - bl[1]) * u];
        out.push([top[0] + (bottom[0] - top[0]) * v, top[1] + (bottom[1] - top[1]) * v]);
      }
    }
    return out;
  }

  // Which cells of a grid lie in open water or on ice, and which warned regions hold the centre.
  const maskCache = new Map();
  function priorMask(grid) {
    const key = JSON.stringify([grid.corners, grid.rows, grid.cols]);
    if (!maskCache.has(key)) {
      maskCache.set(key, (async () => {
        const centres = cellCentres(grid);
        const water = new Uint8Array(centres.length), ice = new Uint8Array(centres.length);
        const tiles = new Map();
        for (const [i, [lon, lat]] of centres.entries()) {
          const k = `${Math.floor(lat)}|${Math.floor(lon)}`;
          if (!tiles.has(k)) tiles.set(k, await geoTile(lat, lon));
          const tile = tiles.get(k);
          if (!tile) continue;
          if (inAny(tile.w, lon, lat)) water[i] = 1;
          else if (inAny(tile.i, lon, lat)) ice[i] = 1;
        }
        const [lon, lat] = centres[Math.floor(centres.length / 2)];
        const tile = await geoTile(lat, lon);
        const regions = tile ? tile.n.filter((f) => PRIOR_WARN.includes(f.k) && inAny(f.p, lon, lat)) : [];
        return { water, ice, regions };
      })());
      maskCache.get(key).catch(() => maskCache.delete(key));
    }
    return maskCache.get(key);
  }

  const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d;

  // Applies the mask to one layer. Statistics move by whole cells from the API's own km2 values,
  // so untouched values stay exactly as the API reported them.
  function correctLayer(layer, mask, cellKm2) {
    const { rows, cols } = layer.grid;
    const groups = layer.grid.groups.map((row) => row.slice());
    const classes = layer.grid.classes.map((row) => row.slice());
    const dGroup = {}, dClass = {};
    const move = (d, from, to) => { d[from] = (d[from] || 0) - cellKm2; if (to) d[to] = (d[to] || 0) + cellKm2; };
    let relabelled = 0, removed = 0;
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const i = r * cols + c, g = groups[r][c], k = classes[r][c];
        if (mask.ice[i]) {
          move(dGroup, GROUPS[g]); move(dClass, CLASSES[k]);
          groups[r][c] = -1; classes[r][c] = -1;
          removed++;
        } else if (mask.water[i] && GROUPS[g] !== "water") {
          move(dGroup, GROUPS[g], "water"); move(dClass, CLASSES[k], "SeaLake");
          groups[r][c] = GROUPS.indexOf("water"); classes[r][c] = CLASSES.indexOf("SeaLake");
          relabelled++;
        }
      }
    }
    if (!relabelled && !removed) return { layer, relabelled, removed };
    const total = layer.stats.total_km2 - removed * cellKm2;
    const fix = (items, d) => {
      const out = items.map((it) => ({ ...it, km2: Math.max(0, it.km2 + (d[it.key] || 0)) }));
      Object.keys(d).forEach((key) => { if (!out.some((it) => it.key === key) && d[key] > 0) out.push({ key, km2: d[key] }); });
      return out.map((it) => ({ ...it, km2: round(it.km2, 2), share: total > 0 ? round((it.km2 / total) * 100, 1) : 0 }));
    };
    return {
      relabelled, removed,
      layer: { ...layer, grid: { ...layer.grid, groups, classes },
        stats: { ...layer.stats, total_km2: round(total, 2), groups: fix(layer.stats.groups, dGroup), classes: fix(layer.stats.classes, dClass) } },
    };
  }

  // Change results: corrected cells can no longer count as changed.
  function correctCompare(body, mask) {
    const cellKm2 = body.cell_km2 ?? (body.cell_m / 1000) ** 2;
    const a = correctLayer(body.before, mask, cellKm2), b = correctLayer(body.after, mask, cellKm2);
    if (a.layer === body.before && b.layer === body.after) return { body, relabelled: 0, removed: 0 };
    const cols = body.before.grid.cols;
    let changedKm2 = body.changed_km2, builtKm2 = body.built_km2, relabelled = 0;
    const moves = new Map(body.transitions.map((m) => [`${m.from}>${m.to}`, { ...m }]));
    const changed = body.changed.map((row, r) => row.map((v, c) => {
      const i = r * cols + c;
      const gb = GROUPS[body.before.grid.groups[r][c]], ga = GROUPS[body.after.grid.groups[r][c]];
      if (mask.water[i] && (gb !== "water" || ga !== "water")) relabelled++;
      if (!v || !(mask.water[i] || mask.ice[i])) return v;
      changedKm2 -= cellKm2;
      if (ga === "built" && gb !== "built") builtKm2 -= cellKm2;
      const m = moves.get(`${gb}>${ga}`);
      if (m) m.km2 -= cellKm2;
      return 0;
    }));
    const transitions = [...moves.values()].filter((m) => m.km2 > 0.005)
      .map((m) => ({ ...m, km2: round(m.km2, 2) })).sort((x, y) => y.km2 - x.km2);
    return {
      relabelled, removed: Math.max(a.removed, b.removed),
      body: { ...body, before: a.layer, after: b.layer, changed, transitions,
        changed_km2: round(Math.max(0, changedKm2), 2), built_km2: round(Math.max(0, builtKm2), 2) },
    };
  }

  // Raw API body -> { body, prior } for the current setting. Without geo data the raw body is used.
  async function withPrior(mode, raw) {
    try {
      const grid = mode === "change" ? raw.before.grid : raw.grid;
      const mask = await priorMask(grid);
      const result = mode === "change" ? correctCompare(raw, mask) : correctLayer(raw, mask, raw.cell_km2 ?? (raw.cell_m / 1000) ** 2);
      const prior = { relabelled: result.relabelled, removed: result.removed, regions: mask.regions };
      return { body: state.prior ? (result.body ?? result.layer) : raw, prior };
    } catch {
      return { body: raw, prior: null };
    }
  }

  // Encoded overlays are cached per layer, colouring and theme, so the animation does not redraw them.
  const overlayCache = new WeakMap();
  function overlayUrl(side, changed) {
    const key = `${changed ? "changed" : state.color}|${state.theme}`;
    if (!overlayCache.has(side)) overlayCache.set(side, new Map());
    const cache = overlayCache.get(side);
    if (!cache.has(key)) cache.set(key, gridCanvas(side, changed).toDataURL());
    return cache.get(key);
  }

  function paintSide(target, side, changed) {
    setImage(target, "scene", side.image.src, side.image.corners, { "raster-fade-duration": 0 });
    setImage(target, "cells", overlayUrl(side, changed), side.grid.corners,
      { "raster-opacity": state.opacity, "raster-resampling": "nearest", "raster-fade-duration": 0 });
    target.setPaintProperty("cells", "raster-opacity", state.opacity);
  }

  // In split view the main map holds the earlier date and the clipped map the later one.
  function showOnMap() {
    const { mode, body } = state.data;
    $("map-year").hidden = mode !== "time" || !body.points[state.timeYear];
    if (mode === "time") {
      const side = body.points[state.timeYear];
      setSplit(false);
      if (!side) return null;
      $("map-year").textContent = state.timeYear;
      paintSide(map, side, null);
      renderLegend(mode, false);
      document.querySelectorAll("#color button").forEach((b) => { b.disabled = false; });
      setFrame(side.image.corners);
      return side;
    }
    const split = mode === "change" && state.layer === "split";
    const side = mode === "now" ? body : split || state.layer === "before" ? body.before : body.after;
    const changeLayer = mode === "change" && state.layer === "change";
    paintSide(map, side, changeLayer ? body.changed : null);
    renderLegend(mode, changeLayer);
    document.querySelectorAll("#color button").forEach((b) => { b.disabled = changeLayer; });
    setFrame(side.image.corners);
    setSplit(split);
    if (split) {
      ensureAfterMap().then((target) => {
        if (state.data && state.data.body === body && state.layer === "split") paintSide(target, body.after, null);
      });
    }
    return side;
  }

  /* ------------------------------------------------------------------ results */

  const swatch = (g) => `<i class="swatch" style="background:var(--c-${g})"></i>`;

  function bars(statsGroups) {
    return `<ul class="bars">${statsGroups.map((g) => `
      <li>
        <div class="head"><span class="name">${swatch(g.key)}${esc(t(g.key))}</span>
          <span class="delta">${pct(g.share)} &nbsp; ${num(g.km2)} km²</span></div>
        <div class="track"><i style="width:${g.share}%;background:var(--c-${g.key})"></i></div>
      </li>`).join("")}</ul>`;
  }

  function classTable(classes) {
    const rows = classes.filter((c) => c.share > 0).sort((a, b) => b.share - a.share)
      .map((c) => `<tr><td><span class="name">${swatch(c.key)}${esc(t(c.key))}</span></td><td>${pct(c.share)}</td><td>${num(c.km2)} km²</td></tr>`).join("");
    return `<details><summary>${esc(t("details"))}</summary><table><tbody>${rows}</tbody></table></details>`;
  }

  // What the Natural Earth prior did here, with a switch back to the raw model output.
  function priorNotice(prior) {
    if (!prior) return "";
    const lines = [];
    if (prior.relabelled) lines.push(t(state.prior ? "priorWater" : "priorWaterOff").replace("{n}", prior.relabelled));
    if (prior.removed) lines.push(t(state.prior ? "priorIce" : "priorIceOff").replace("{n}", prior.removed));
    prior.regions.forEach((r) => lines.push(t(`priorRegion_${r.k}`).replace("{name}", r.name[state.lang] || r.name.en)));
    if (!lines.length) return "";
    const toggle = prior.relabelled || prior.removed ? `
      <label class="check"><input type="checkbox" id="prior-toggle"${state.prior ? " checked" : ""}>
        <span>${esc(t("priorToggle"))}</span></label>` : "";
    return `<div class="notice prior">${lines.map((l) => `<p>${esc(l)}</p>`).join("")}${toggle}</div>`;
  }

  function notices(...sides) {
    const out = [];
    if (state.data.mode === "change" && state.data.body.season_warning) out.push(t("seasonWarning"));
    if (sides.some((s) => s.unusable_share > 5)) out.push(t("unusableWarning"));
    return out.map((m) => `<p class="notice">${esc(m)}</p>`).join("");
  }

  const opacityControl = () => `
    <label class="range"><span>${esc(t("opacity"))}</span>
      <input id="opacity" type="range" min="0" max="100" value="${Math.round(state.opacity * 100)}"></label>`;

  const footer = () => `
    <div class="actions">
      <button class="pill-button" id="copy" type="button">${esc(t("share_link"))}</button>
      <button class="pill-button" id="again" type="button">${esc(t("again"))}</button>
    </div>
    <p class="limits">${esc(t("limits"))}</p>`;

  // On phones the panel is a bottom sheet: the head (title and main figure) stays in view.
  const sheetHead = (figure) => `
    <div class="sheet-head" id="sheet-head">
      <button type="button" class="grabber" id="sheet-toggle" aria-controls="sheet-body"><i></i></button>
      <h2>${esc(state.data.label)}</h2>
      ${figure}
    </div>`;

  function renderNow(body) {
    const built = body.stats.groups.find((g) => g.key === "built");
    return `
      ${sheetHead(built ? `<p class="figure"><strong>${pct(built.share)}</strong><span>${esc(t("builtShare"))}</span></p>` : "")}
      <div class="sheet-body" id="sheet-body">
      <p class="meta">${esc(t("imageDate"))}: ${esc(body.date)} &nbsp; ${esc(t("totalArea"))}: ${num(body.stats.total_km2, 0)} km²</p>
      ${notices(body)}
      ${priorNotice(state.data.prior)}
      ${bars(body.stats.groups)}
      ${classTable(body.stats.classes)}
      ${colorControl(body)}
      ${opacityControl()}
      ${footer()}
      </div>`;
  }

  function renderChange(body) {
    const a = body.before, b = body.after;
    const rows = a.stats.groups.map((g, i) => {
      const after = b.stats.groups[i];
      const delta = after.share - g.share;
      // Shares arrive with one decimal; round so 29.3 → 28.3 counts as exactly one point.
      const trend = Math.abs(Math.round(delta * 10) / 10) < NOISE_PTS ? "trendFlat" : delta > 0 ? "trendUp" : "trendDown";
      return `<tr><td><span class="name">${swatch(g.key)}${esc(t(g.key))}</span>
          <small class="trend">${esc(t(trend))}</small></td>
        <td>${pct(g.share)}</td><td>${pct(after.share)}</td>
        <td>${delta > 0 ? "+" : ""}${num(delta)}</td></tr>`;
    }).join("");
    const moves = body.transitions.slice(0, 5)
      .map((m) => `<li><span>${esc(t(m.from))} → ${esc(t(m.to))}</span><span>${num(m.km2)} km²</span></li>`).join("");
    const layerButton = (key) =>
      `<button type="button" data-layer="${key}" aria-selected="${state.layer === key}">${esc(t("layer" + key[0].toUpperCase() + key.slice(1)))}</button>`;

    return `
      ${sheetHead(`<p class="figure"><strong>${num(body.built_km2)} km²</strong><span>${esc(t("builtGain"))}</span></p>`)}
      <div class="sheet-body" id="sheet-body">
      <p class="meta">${esc(a.date)} → ${esc(b.date)} &nbsp; ${esc(t("totalArea"))}: ${num(a.stats.total_km2, 0)} km²<br>
        ${esc(t("changedTotal"))}: ${num(body.changed_km2)} km²</p>
      ${state.data.story ? storyBlock(state.data.story) : ""}
      ${notices(a, b)}
      ${priorNotice(state.data.prior)}
      <div><p class="subhead">${esc(t("layer"))}</p>
        <div class="segmented small" id="layer">${["split", "before", "after", "change"].map(layerButton).join("")}</div>
        <div class="sides" id="sides"${state.layer === "split" ? "" : " hidden"}>
          <p><span>${esc(t("splitLeft"))}</span><strong>${esc(t("before"))} · ${esc(a.date)}</strong></p>
          <p><span>${esc(t("splitRight"))}</span><strong>${esc(t("after"))} · ${esc(b.date)}</strong></p>
          <p class="hint">${esc(t("splitHint"))}</p>
        </div></div>
      ${colorControl(a, b)}
      ${opacityControl()}
      <table><thead><tr><th></th><th>${esc(a.date.slice(0, 4))}</th><th>${esc(b.date.slice(0, 4))}</th><th>±</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div><p class="subhead">${esc(t("transitions"))}</p>
        ${moves ? `<ul class="transitions">${moves}</ul>` : `<p class="meta">${esc(t("noChange"))}</p>`}</div>
      ${footer()}
      </div>`;
  }

  function renderTime() {
    return `
      ${sheetHead(`<div id="time-figure"></div>${playerHtml()}`)}
      <div class="sheet-body" id="sheet-body">
        <div id="timeline"></div>
        <div id="time-color"></div>
        ${opacityControl()}
        ${footer()}
      </div>`;
  }

  // Main figure: built-up share change between the first and last year that have an image.
  function timeFigure(body) {
    const loaded = loadedYears(body);
    if (loaded.length < 2) return `<p class="figure"><strong>…</strong></p>`;
    const first = loaded[0], last = loaded.at(-1);
    const delta = groupShare(body.points[last], "built") - groupShare(body.points[first], "built");
    return `<p class="figure"><strong>${delta > 0 ? "+" : ""}${num(delta)}</strong>
      <span>${esc(t("builtTrend"))} (${esc(t("builtTrendRange").replace("{from}", first).replace("{to}", last))})</span></p>`;
  }

  function timelineHtml(body) {
    const loaded = loadedYears(body);
    const any = loaded.length ? body.points[loaded[0]] : null;
    const faded = loaded.filter((y) => fadedYear(body.points[y]));
    const notes = [
      body.missing.length && t("missingYears").replace("{years}", body.missing.join(", ")),
      body.failed.length && t("failedYears").replace("{years}", body.failed.join(", ")),
      faded.length && t("fadedYears").replace("{years}", faded.join(", ")),
    ].filter(Boolean);
    const rows = body.years.map((y) => {
      const p = body.points[y];
      if (p === undefined) return `<tr><td>${y}</td><td colspan="5">${esc(t("yearPending"))}</td></tr>`;
      if (!p) return `<tr><td>${y}</td><td colspan="5">${esc(t(body.failed.includes(y) ? "yearFailed" : "yearMissing"))}</td></tr>`;
      return `<tr><td>${y}</td><td>${esc(p.date)}</td>${GROUPS.map((g) => `<td>${pct(groupShare(p, g))}</td>`).join("")}</tr>`;
    }).join("");
    return `
      <p class="meta">${esc(t("yearsMeta").replace("{n}", body.years.length))}${any ? ` &nbsp; ${esc(t("totalArea"))}: ${num(any.stats.total_km2, 0)} km²` : ""}</p>
      ${notes.map((n) => `<p class="notice">${esc(n)}</p>`).join("")}
      ${priorNotice(body.priors[state.timeYear])}
      <div><p class="subhead">${esc(t("chartTitle"))}</p>
        <div class="chart" id="chart" tabindex="0" role="group" aria-label="${esc(t("chartLabel"))}">
          ${chartSvg(body)}
          <div class="chart-tip" id="chart-tip" hidden></div>
        </div>
        <p class="hint">${esc(t("chartHint"))}</p>
        <p class="visually-hidden" id="chart-live" aria-live="polite"></p></div>
      <details><summary>${esc(t("tableView"))}</summary>
        <table class="years"><thead><tr><th>${esc(t("year"))}</th><th>${esc(t("date"))}</th>${GROUPS.map((g) => `<th>${swatch(g)}</th>`).join("")}</tr></thead>
        <tbody>${rows}</tbody></table></details>`;
  }

  // Hand-written SVG line chart: one line per group, direct labels at the line ends,
  // gaps where a year has no image, faded marks where too much of the image is unusable.
  const CHART = { W: 360, H: 210, L: 30, R: 104, T: 10, B: 24 };

  function chartX(body, year) {
    const { W, L, R } = CHART;
    const first = body.years[0], last = body.years.at(-1);
    return last === first ? (L + W - R) / 2 : L + ((W - L - R) * (year - first)) / (last - first);
  }

  function chartSvg(body) {
    const { W, H, L, R, T, B } = CHART;
    const loaded = loadedYears(body);
    const values = loaded.flatMap((y) => GROUPS.map((g) => groupShare(body.points[y], g)));
    const peak = Math.max(10, ...values);
    const step = [5, 10, 20, 25].find((s) => peak / s <= 5) || 25;
    const top = Math.min(100, Math.ceil(peak / step) * step);
    const y = (v) => T + (H - T - B) * (1 - v / top);
    const x = (yr) => chartX(body, yr);
    const colW = body.years.length > 1 ? (W - L - R) / (body.years.length - 1) : 40;
    const out = [];

    for (let v = 0; v <= top; v += step) {
      out.push(`<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`,
        `<text class="axis" x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${num(v, 0)}</text>`);
    }
    // Label every other year on long ranges, always keeping the first and the last.
    const n = body.years.length, every = n > 8 ? 2 : 1;
    const labelled = new Set(body.years.map((_, i) => i).filter((i) => i % every === 0));
    if (!labelled.has(n - 1)) { labelled.delete(n - 1 - ((n - 1) % every)); labelled.add(n - 1); }
    body.years.forEach((yr, i) => {
      const gap = body.points[yr] === null;
      if (gap) out.push(`<rect class="gap" x="${x(yr) - colW / 2}" y="${T}" width="${colW}" height="${H - T - B}"/>`);
      if (labelled.has(i) || gap) {
        out.push(`<text class="axis${gap ? " muted" : ""}" x="${x(yr)}" y="${H - 6}" text-anchor="middle">${yr}</text>`);
      }
    });
    if (loaded.includes(state.timeYear)) {
      out.push(`<line class="pick" x1="${x(state.timeYear)}" x2="${x(state.timeYear)}" y1="${T}" y2="${H - B}"/>`);
    }

    const ends = [];
    GROUPS.forEach((g) => {
      const colour = `var(--c-${g})`;
      body.years.forEach((yr, i) => {
        const next = body.years[i + 1];
        const a = body.points[yr], b = body.points[next];
        if (a && b) {
          const dim = fadedYear(a) || fadedYear(b);
          out.push(`<line class="series${dim ? " faded" : ""}" stroke="${colour}" x1="${x(yr)}" y1="${y(groupShare(a, g))}" x2="${x(next)}" y2="${y(groupShare(b, g))}"/>`);
        }
      });
      loaded.forEach((yr) => {
        const p = body.points[yr];
        out.push(`<circle class="dot${fadedYear(p) ? " faded" : ""}" fill="${colour}" cx="${x(yr)}" cy="${y(groupShare(p, g))}" r="${yr === state.timeYear ? 4 : 2.5}"/>`);
      });
      if (loaded.length) {
        const last = loaded.at(-1);
        ends.push({ g, x: x(last) + 7, y: y(groupShare(body.points[last], g)) + 3 });
      }
    });
    // Keep the end labels from overlapping.
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 12);
    const overflow = ends.length ? ends.at(-1).y - (H - B) : 0;
    if (overflow > 0) ends.forEach((e) => { e.y -= overflow; });
    ends.forEach((e) => out.push(`<text class="end" x="${e.x}" y="${e.y}" fill="var(--c-${e.g})">${esc(t(e.g))}</text>`));

    body.years.forEach((yr) => {
      out.push(`<rect class="hit" data-year="${yr}" x="${x(yr) - colW / 2}" y="${T}" width="${colW}" height="${H - T}"/>`);
    });
    return `<svg viewBox="0 0 ${W} ${H}" aria-hidden="true">${out.join("")}</svg>`;
  }

  function showTip(year) {
    const tip = $("chart-tip"), chart = $("chart");
    if (!tip || year == null) return;
    const body = state.data.body;
    const p = body.points[year];
    tip.innerHTML = p
      ? `<strong>${year}</strong> <span class="meta">${esc(p.date)}</span>
         ${GROUPS.map((g) => `<span class="row">${swatch(g)}${esc(t(g))}<b>${pct(groupShare(p, g))}</b></span>`).join("")}
         ${fadedYear(p) ? `<span class="meta">${esc(t("yearFaded"))}</span>` : ""}`
      : `<strong>${year}</strong> <span class="meta">${esc(p === undefined ? t("yearPending") : t(body.failed.includes(year) ? "yearFailed" : "yearMissing"))}</span>`;
    tip.hidden = false;
    const share = chartX(body, year) / CHART.W;
    const width = chart.clientWidth;
    const left = Math.min(Math.max(share * width - tip.offsetWidth / 2, 0), width - tip.offsetWidth);
    tip.style.left = `${left}px`;
  }

  function hideTip() {
    const tip = $("chart-tip");
    if (tip && document.activeElement !== $("chart")) tip.hidden = true;
  }

  // Chart interaction, delegated because the chart is redrawn as years arrive.
  const results = $("results");
  results.addEventListener("pointermove", (e) => {
    const hit = e.target.closest("#chart .hit");
    if (hit) showTip(Number(hit.dataset.year));
  });
  results.addEventListener("pointerout", (e) => {
    if (e.target.closest("#chart") && !e.relatedTarget?.closest?.("#chart")) hideTip();
  });
  results.addEventListener("click", (e) => {
    const hit = e.target.closest("#chart .hit");
    if (!hit) return;
    const year = Number(hit.dataset.year);
    if (state.data.body.points[year]) selectYear(year, false);
    showTip(year);
  });
  results.addEventListener("keydown", (e) => {
    if (e.target.id !== "chart") return;
    const loaded = loadedYears(state.data.body);
    if (!loaded.length) return;
    const i = loaded.indexOf(state.timeYear);
    const next = { ArrowLeft: loaded[Math.max(0, i - 1)], ArrowRight: loaded[Math.min(loaded.length - 1, i + 1)],
      Home: loaded[0], End: loaded.at(-1) }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    selectYear(next, true);
    showTip(next);
  });
  results.addEventListener("focusin", (e) => { if (e.target.id === "chart") showTip(state.timeYear); });
  results.addEventListener("focusout", (e) => { if (e.target.id === "chart") $("chart-tip").hidden = true; });

  function storyBlock(story) {
    const text = story.description[state.lang] || "";
    const html = `${text ? `<p>${esc(text)}</p>` : ""}${storySources(story)}`;
    return html ? `<div class="story-text">${html}</div>` : "";
  }

  // Entries the current layer does not draw are dimmed rather than removed.
  function renderLegend(mode, changeLayer) {
    const legend = $("legend");
    legend.setAttribute("aria-label", t("legend"));
    legend.hidden = false;
    if (state.color === "confidence" && !changeLayer) {
      legend.innerHTML = `<li data-key="lowconf">${swatch("lowconf")}${esc(t("legendConfidence"))}</li>`;
      return;
    }
    const base = state.color === "classes" ? CLASS_ORDER : GROUPS;
    const keys = mode === "change" ? [...base, "changed"] : base;
    legend.innerHTML = keys.map((k) =>
      `<li data-key="${k}"${changeLayer !== (k === "changed") ? ' class="off"' : ""}>${swatch(k)}${esc(t(k))}</li>`).join("");
  }

  function colorControl(...sides) {
    const button = (key) =>
      `<button type="button" data-color="${key}" aria-selected="${state.color === key}">${esc(t("color" + key[0].toUpperCase() + key.slice(1)))}</button>`;
    const low = sides.map((s) => `${pct(s.stats.low_confidence_share)}`).join(" → ");
    return `<div><p class="subhead">${esc(t("colorBy"))}</p>
      <div class="segmented small" id="color">${["groups", "classes", "confidence"].map(button).join("")}</div>
      <p class="meta">${esc(t("lowConfidence"))}: ${low}</p></div>`;
  }

  function renderResults() {
    const { mode, body } = state.data;
    const panel = $("results");
    panel.innerHTML = mode === "now" ? renderNow(body) : mode === "change" ? renderChange(body) : renderTime(body);
    if (mode === "change") {
      $("split-tag-before").textContent = body.before.date;
      $("split-tag-after").textContent = body.after.date;
    }
    if (mode === "time") {
      state.data.paintedYear = undefined;
      updateTimeline();
    }
    const side = showOnMap();
    if (side && !state.data.fitted) {
      fit(side.image.corners, 900);
      state.data.fitted = true;
    }

    $("opacity").addEventListener("input", (e) => {
      state.opacity = Number(e.target.value) / 100;
      maps().forEach((target) => { if (target.getLayer("cells")) target.setPaintProperty("cells", "raster-opacity", state.opacity); });
    });
    $("again").addEventListener("click", () => { setSheet(false); searchInput.value = ""; searchInput.focus(); });
    $("copy").addEventListener("click", async (e) => {
      try { await navigator.clipboard.writeText(location.href); e.target.textContent = t("copied"); } catch { /* clipboard blocked */ }
    });

    applySheet();
  }

  // Switching the prior re-derives the shown results from the raw API answers.
  $("results").addEventListener("change", async (e) => {
    if (e.target.id !== "prior-toggle") return;
    state.prior = e.target.checked;
    store.set("prior", state.prior ? "on" : "off");
    const data = state.data;
    if (data.mode === "time") {
      const series = data.body;
      for (const year of Object.keys(series.raw)) {
        const { body, prior } = await withPrior("time", series.raw[year]);
        series.points[year] = body;
        series.priors[year] = prior;
      }
      data.paintedYear = undefined;
      updateTimeline();
    } else {
      const { body, prior } = await withPrior(data.mode, data.raw);
      data.body = body;
      data.prior = prior;
      renderResults();
    }
    $("prior-toggle")?.focus();
  });

  // Panel controls are re-rendered often, so their clicks are handled once, here.
  $("results").addEventListener("click", (e) => {
    const colour = e.target.closest("#color button");
    if (colour && !colour.disabled) {
      state.color = colour.dataset.color;
      document.querySelectorAll("#color button").forEach((x) => x.setAttribute("aria-selected", String(x === colour)));
      showOnMap();
    }
    const layer = e.target.closest("#layer button");
    if (layer) {
      state.layer = layer.dataset.layer;
      document.querySelectorAll("#layer button").forEach((x) => x.setAttribute("aria-selected", String(x === layer)));
      $("sides").hidden = state.layer !== "split";
      showOnMap();
    }
  });

  /* ------------------------------------------------------------------ phone bottom sheet */

  const sheet = $("results");
  let sheetOpen = false;

  // Below 860px the panel is a sheet; the closed body is inert so Tab skips what is off screen.
  function applySheet() {
    const head = $("sheet-head"), body = $("sheet-body"), toggle = $("sheet-toggle");
    if (!head) return;
    const phone = !wide();
    sheet.classList.toggle("open", sheetOpen);
    body.inert = phone && !sheetOpen;
    toggle.setAttribute("aria-expanded", String(sheetOpen));
    toggle.setAttribute("aria-label", t(sheetOpen ? "sheetClose" : "sheetOpen"));
    const peek = phone && !sheet.hidden ? head.offsetHeight : 0;
    document.documentElement.style.setProperty("--sheet-peek", `${peek}px`);
  }

  function setSheet(open) {
    sheetOpen = open;
    applySheet();
  }

  let drag = null;
  let dragged = false;
  sheet.addEventListener("pointerdown", (e) => {
    dragged = false;
    if (wide() || !e.target.closest(".sheet-head") || e.target.closest(".player")) return;   // controls in the head stay usable
    const closed = sheet.offsetHeight - $("sheet-head").offsetHeight;
    drag = { id: e.pointerId, y: e.clientY, from: sheetOpen ? 0 : closed, closed, offset: 0 };
  });
  sheet.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dy = e.clientY - drag.y;
    if (!dragged && Math.abs(dy) < 6) return;
    if (!dragged) {
      dragged = true;
      sheet.setPointerCapture(e.pointerId);
      sheet.classList.add("dragging");
    }
    drag.offset = Math.min(drag.closed, Math.max(0, drag.from + dy));
    sheet.style.transform = `translateY(${drag.offset}px)`;
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    sheet.classList.remove("dragging");
    sheet.style.transform = "";
    if (dragged) setSheet(d.offset < d.closed / 2);
    // A tap on the head toggles; taps on the grabber button arrive as its click instead.
    else if (e.type === "pointerup" && !e.target.closest("#sheet-toggle")) setSheet(!sheetOpen);
  };
  sheet.addEventListener("pointerup", endDrag);
  sheet.addEventListener("pointercancel", endDrag);
  sheet.addEventListener("click", (e) => {
    if (e.target.closest("#sheet-toggle") && !dragged) setSheet(!sheetOpen);
  });
  sheet.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sheetOpen && !wide()) {
      setSheet(false);
      $("sheet-toggle").focus();
    }
  });
  addEventListener("resize", applySheet);

  /* ------------------------------------------------------------------ sections below the map */

  // Examples, the city ranking and "how it works" are always-open sections under the map.
  const SECTIONS = { examples: renderStories, rankings: renderRankings, about: renderAbout };
  // Data files are small scripts that register themselves on window.LC_DATA (see scripts/datajs.py),
  // so they also load when index.html is opened straight from disk, where fetch() is blocked.
  const dataCache = new Map();

  function loadData(name) {
    if (!dataCache.has(name)) {
      const loading = new Promise((resolve, reject) => {
        const el = document.createElement("script");
        el.src = `data/${name}.js`;
        el.onload = () => {
          const data = window.LC_DATA && window.LC_DATA[name];
          if (data) resolve(data); else reject(new Error(name));
        };
        el.onerror = () => reject(new Error(name));
        document.head.append(el);
      });
      loading.catch(() => dataCache.delete(name));
      dataCache.set(name, loading);
    }
    return dataCache.get(name);
  }

  function renderSections() {
    Object.entries(SECTIONS).forEach(async ([id, render]) => {
      const body = $(`${id}-body`);
      try {
        body.innerHTML = await render();
      } catch {
        body.innerHTML = `<p class="notice">${esc(t("errData"))}</p>`;
      }
    });
  }

  const scrollOpts = () => ({ behavior: reducedMotion ? "auto" : "smooth" });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-scroll]");
    if (b) $(b.dataset.scroll).scrollIntoView({ ...scrollOpts(), block: "start" });
  });

  // After picking something in a section, bring the map back into view.
  const backToMap = () => scrollTo({ top: 0, ...scrollOpts() });

  const countryName = (code) => {
    try { return new Intl.DisplayNames([state.lang], { type: "region" }).of(code); } catch { return code; }
  };

  /* ------------------------------------------------------------------ city rankings */

  const CONTINENTS = ["europe", "asia", "africa", "north_america", "south_america", "oceania"];
  const continentName = (key) => t(`continent_${key}`);
  const cityName = (c) => (c.names && c.names[state.lang]) || c.name;
  const rankView = { sort: "built_change_pts", dir: -1, continent: "" };

  async function renderRankings() {
    const data = await loadData("rankings");
    const p = data.params;
    const all = [...data.cities, ...data.unavailable];
    const continents = CONTINENTS.filter((k) => all.some((c) => c.continent === k));
    const reason = (c) => {
      if (c.reason === "unusable") return t("reasonUnusable").replace("{a}", pct(c.unusable_share[0])).replace("{b}", pct(c.unusable_share[1]));
      if (c.reason === "season") return t("reasonSeason").replace("{n}", num(c.season_gap_days, 0));
      return t({ no_imagery: "reasonNoImagery", invalid: "reasonInvalid" }[c.reason] || "reasonServer");
    };
    return `
      <p class="notice">${esc(t("rankLimits"))}</p>
      <p class="meta">${esc(t("rankMethod").replace("{before}", p.before).replace("{after}", p.after)
        .replace("{size}", p.size_km).replace("{cell}", p.cell_m)
        .replace("{unusable}", num(p.max_unusable)).replace("{gap}", p.max_season_gap_days))}
        ${esc(t("generatedOn").replace("{date}", data.generated))}</p>
      ${data.reviewed ? "" : `<p class="notice">${esc(t("rankUnreviewed"))}</p>`}
      <label class="filter"><span>${esc(t("continent"))}</span>
        <select id="rank-continent" class="pill-select">
          <option value="">${esc(t("allContinents"))}</option>
          ${continents.map((k) => `<option value="${k}"${k === rankView.continent ? " selected" : ""}>${esc(continentName(k))}</option>`).join("")}
        </select></label>
      <div id="ranking-table">${rankingTable(data)}</div>
      ${data.unavailable.length ? `<div><p class="subhead">${esc(t("rankUnavailable"))}</p>
        <ul class="unavailable">${data.unavailable.map((c) => `
          <li><span>${esc(cityName(c))}, ${esc(countryName(c.country))}</span><span>${esc(reason(c))}</span></li>`).join("")}</ul></div>` : ""}`;
  }

  function rankingTable(data) {
    const p = data.params;
    const rows = data.cities
      .filter((c) => !rankView.continent || c.continent === rankView.continent)
      .sort((a, b) => {
        // Continents sort in a fixed geographic order, then by gain within each continent.
        if (rankView.sort === "continent") {
          const d = CONTINENTS.indexOf(a.continent) - CONTINENTS.indexOf(b.continent);
          return d ? rankView.dir * d : b.built_change_pts - a.built_change_pts;
        }
        return rankView.dir * (a[rankView.sort] - b[rankView.sort]);
      });
    const sortHead = (key, label) => {
      const on = rankView.sort === key;
      return `<th aria-sort="${on ? (rankView.dir < 0 ? "descending" : "ascending") : "none"}">
        <button type="button" class="sort" data-sort="${key}">${esc(label)}${on ? (rankView.dir < 0 ? " ↓" : " ↑") : ""}</button></th>`;
    };
    if (!rows.length) return `<p class="meta">${esc(t("noResults"))}</p>`;
    return `<table class="rankings">
      <thead><tr><th>#</th><th>${esc(t("city"))}</th>${sortHead("continent", t("continent"))}<th>${esc(p.before)}</th><th>${esc(p.after)}</th>
        ${sortHead("built_change_pts", t("rankGain"))}${sortHead("built_km2", t("rankKm2"))}</tr></thead>
      <tbody>${rows.map((c, i) => `
        <tr data-city="${data.cities.indexOf(c)}">
          <td>${i + 1}</td>
          <td><button type="button" class="row-link" data-city="${data.cities.indexOf(c)}">${esc(cityName(c))}</button>
            <small>${esc(countryName(c.country))}</small>${c.experimental ? ` <span class="tag">${esc(t("experimental"))}</span>` : ""}</td>
          <td>${esc(continentName(c.continent))}</td>
          <td>${pct(c.built_before)}</td><td>${pct(c.built_after)}</td>
          <td>${c.built_change_pts > 0 ? "+" : ""}${num(c.built_change_pts)}</td><td>${num(c.built_km2)}</td>
        </tr>`).join("")}</tbody></table>`;
  }

  // Opens the change report for a ranked city with the same settings the ranking used.
  function openCityReport(city, params) {
    backToMap();
    $("size").value = String(params.size_km);
    setCell(params.cell_m);
    setMode("change");
    $("before").value = params.before;
    $("after").value = params.after;
    setPlace(city.lat, city.lon, cityName(city));
    $("controls").requestSubmit();
  }

  $("rankings-body").addEventListener("click", async (e) => {
    const data = await loadData("rankings");
    const sort = e.target.closest("button[data-sort]");
    if (sort) {
      const key = sort.dataset.sort;
      rankView.dir = rankView.sort === key ? -rankView.dir : key === "continent" ? 1 : -1;
      rankView.sort = key;
      $("ranking-table").innerHTML = rankingTable(data);
      $("ranking-table").querySelector(`[data-sort="${key}"]`).focus();
      return;
    }
    const row = e.target.closest("[data-city]");
    if (row) openCityReport(data.cities[Number(row.dataset.city)], data.params);
  });
  $("rankings-body").addEventListener("change", async (e) => {
    if (e.target.id !== "rank-continent") return;
    rankView.continent = e.target.value;
    $("ranking-table").innerHTML = rankingTable(await loadData("rankings"));
  });

  /* ------------------------------------------------------------------ stories */

  // Ready-made reports: the API response is saved in docs/data/stories, so they open without waiting.
  const storyTitle = (story) => story.title[state.lang] || story.title.en;
  const safeUrl = (url) => (/^https?:\/\//.test(url) ? url : null);

  function storySources(story) {
    const links = (story.sources || []).filter((s) => safeUrl(s.url))
      .map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>`);
    return links.length ? `<p class="sources">${esc(t("sources"))}: ${links.join(", ")}</p>` : "";
  }

  async function renderStories() {
    const { stories } = await loadData("stories");
    const card = (s) => {
      const text = s.description[state.lang] || "";
      return `<li class="story">
        <button type="button" class="story-open" data-story="${esc(s.id)}">
          <img src="data/stories/${esc(s.id)}-thumb.jpg" alt="" loading="lazy" width="320" height="240">
          <strong>${esc(storyTitle(s))}</strong>
          <span class="meta">${esc(s.dates.before)} → ${esc(s.dates.after)} &nbsp; ${num(s.location.size_km, 0)} km
            ${s.experimental ? `<span class="tag">${esc(t("experimental"))}</span>` : ""}</span>
        </button>
        ${text ? `<p>${esc(text)}</p>` : ""}
        ${storySources(s)}</li>`;
    };
    const groups = ["turkey", ...CONTINENTS].filter((g) => stories.some((s) => s.group === g));
    return `
      <p class="lead">${esc(t("storiesIntro"))}</p>
      <p class="meta">${esc(t("experimentalNote"))}</p>
      ${groups.map((g) => `<div class="story-group"><h3>${esc(g === "turkey" ? t("groupTurkey") : continentName(g))}</h3>
        <ul class="stories">${stories.filter((s) => s.group === g).map(card).join("")}</ul></div>`).join("")}`;
  }

  async function openStory(id) {
    const { stories } = await loadData("stories");
    const story = stories.find((s) => s.id === id);
    if (!story) return false;
    const raw = await loadData(`stories/${id}`);
    const { body, prior } = await withPrior("change", raw);
    await mapReady;
    backToMap();
    stopPlaying();
    const { lat, lon, size_km: size, cell_m: cell } = story.location;
    $("size").value = String(size);
    setCell(cell);
    setMode("change");
    $("before").value = story.dates.before;
    $("after").value = story.dates.after;
    setPlace(lat, lon, storyTitle(story), false);
    state.data = { mode: "change", raw, body, prior, label: storyTitle(story), story };
    state.layer = "split";
    sheetOpen = false;
    setStatus("");
    $("results").hidden = false;
    centreSplit();
    renderResults();
    const link = new URLSearchParams({ lat: lat.toFixed(5), lon: lon.toFixed(5), size_km: size, cell_m: cell,
      before: story.dates.before, after: story.dates.after, story: id });
    updateLink(link);
    return true;
  }

  $("examples-body").addEventListener("click", (e) => {
    const b = e.target.closest("[data-story]");
    if (b) openStory(b.dataset.story).catch(() => setStatus(t("errData"), true));
  });

  /* ------------------------------------------------------------------ how it works */

  // Content follows PLAN.md (project, API contract, known limits) and /api/meta (classes, groups).
  const GROUP_CLASSES = {
    agriculture: ["AnnualCrop", "PermanentCrop", "Pasture"],
    vegetation: ["Forest", "HerbaceousVegetation"],
    built: ["Residential", "Industrial", "Highway"],
    water: ["River", "SeaLake"],
  };

  async function renderAbout() {
    const steps = ["Place", "Image", "Classify", "Compare"].map((k) => `
      <li><strong>${esc(t(`step${k}`))}</strong><span>${esc(t(`step${k}Text`))}</span></li>`).join("");
    const groups = GROUPS.map((g) => `<li><span class="name">${swatch(g)}${esc(t(g))}</span>
      <span>${GROUP_CLASSES[g].map((c) => esc(t(c))).join(", ")}</span></li>`).join("");
    const decisions = ["Groups", "Confidence", "Season", "Clouds", "Prior"].map((k) => `
      <div><p class="subhead">${esc(t(`decision${k}`))}</p><p>${esc(t(`decision${k}Text`))}</p>
      ${k === "Groups" ? `<ul class="group-list">${groups}</ul>` : ""}</div>`).join("");
    const limits = ["Estimate", "Classes", "Europe", "Dates", "Window", "Season", "Basemap"]
      .map((k) => `<li>${esc(t(`limit${k}`))}</li>`).join("");
    return `
      <p class="lead">${esc(t("aboutIntro"))}</p>
      <div><p class="subhead">${esc(t("aboutFlow"))}</p><ol class="flow">${steps}</ol></div>
      <div><p class="subhead">${esc(t("aboutDecisions"))}</p><div class="decisions">${decisions}</div></div>
      <div><p class="subhead">${esc(t("aboutLimits"))}</p><ul class="limits-list">${limits}</ul></div>`;
  }

  /* ------------------------------------------------------------------ shareable link */

  function updateLink(params) {
    const link = new URLSearchParams(params);
    link.set("mode", state.mode);
    link.set("q", state.place.label);
    history.replaceState(null, "", `#${link}`);
  }

  function restoreFromLink() {
    if (location.hash.length < 2) return;
    const p = new URLSearchParams(location.hash.slice(1));
    const lat = Number(p.get("lat")), lon = Number(p.get("lon"));
    if (!p.has("lat") || Number.isNaN(lat) || Number.isNaN(lon)) return;
    if (p.get("size_km")) $("size").value = p.get("size_km");
    if (p.get("cell_m")) setCell(Number(p.get("cell_m")));
    setMode(["change", "time"].includes(p.get("mode")) ? p.get("mode") : "now");
    ["when", "before", "after", "start", "end"].forEach((k) => { if (p.get(k)) $(k).value = p.get(k); });
    setPlace(lat, lon, p.get("q") || `${lat.toFixed(3)}, ${lon.toFixed(3)}`);
    // A story link opens the saved report; if that fails, run the analysis as usual.
    if (p.get("story")) openStory(p.get("story")).then((ok) => { if (!ok) $("controls").requestSubmit(); }, () => $("controls").requestSubmit());
    else $("controls").requestSubmit();
  }

  /* ------------------------------------------------------------------ start */

  const langSelect = $("lang");
  langSelect.innerHTML = Object.entries(LANGS).map(([code, l]) => `<option value="${code}">${esc(l.name)}</option>`).join("");
  langSelect.value = state.lang;
  langSelect.addEventListener("change", () => { state.lang = langSelect.value; store.set("lang", state.lang); applyLanguage(); });

  $("theme").addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    store.set("theme", state.theme);
    applyTheme();
    if (state.data) showOnMap();
  });

  applyTheme();
  applyLanguage();
  setMode("now");
})();
