/* Land Change front end: globe, place search, API calls and result rendering. */
(() => {
  "use strict";

  const API = window.LAND_CHANGE_API;
  const LANGS = window.LANGS;
  const GROUPS = ["agriculture", "vegetation", "built", "water"];
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
    mode: "now",          // "now" | "change"
    cell: 640,
    data: null,           // last API response
    layer: "split",       // what the map shows in change mode: split | before | after | change
    split: 0.5,           // handle position as a share of the map width
    opacity: 0.55,
  };

  /* ------------------------------------------------------------------ language and theme */

  const t = (key) => (LANGS[state.lang] && LANGS[state.lang][key]) ?? LANGS.en[key] ?? key;
  const num = (value, digits = 1) =>
    new Intl.NumberFormat(state.lang, { maximumFractionDigits: digits }).format(value);

  function applyLanguage() {
    document.documentElement.lang = state.lang;
    document.title = t("title");
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    $("run").textContent = t(state.mode === "now" ? "runNow" : "runChange");
    $("theme").textContent = `${t("theme")}: ${t(state.theme)}`;
    $("split-handle").setAttribute("aria-label", t("slider"));
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
    return { top: 90, bottom: 50, left: 440, right: roomy && !$("results").hidden ? 460 : 40 };
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

  map.on("load", () => {
    addFrame(map);
    restoreFromLink();
    spin();
  });

  map.on("click", (e) => {
    const { lat, lng } = e.lngLat;
    setPlace(lat, lng, `${lat.toFixed(3)}, ${lng.toFixed(3)}`);
    fetch(`${PHOTON}/reverse?lat=${lat}&lon=${lng}`)
      .then((r) => r.json())
      .then((j) => {
        const f = j.features && j.features[0];
        if (f && state.place && state.place.lat === lat) { state.place.label = placeLabel(f).full; showPlaceLine(); }
      })
      .catch(() => { /* the coordinate label stays */ });
  });

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
      return showSuggestions([{ name: label, rest: "", full: label, lat, lon }]);
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

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("#mode button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mode === mode)));
    $("fields-now").hidden = mode !== "now";
    $("fields-change").hidden = mode !== "change";
    closeDates();
    $("run").textContent = t(mode === "now" ? "runNow" : "runChange");
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

  function setStatus(message, isError = false) {
    const el = $("status");
    el.hidden = !message;
    el.textContent = message;
    el.classList.toggle("error", isError);
  }

  const validDate = (v) => /^\d{4}(-\d{2}-\d{2})?$/.test(v);

  $("controls").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.place) return setStatus(t("pickFirst"), true);

    const params = new URLSearchParams({
      lat: state.place.lat.toFixed(5), lon: state.place.lon.toFixed(5),
      size_km: $("size").value, cell_m: String(state.cell),
    });
    let path = "/api/analyze";
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

    const button = $("run");
    button.disabled = true;
    setStatus(t("loading"));
    const wake = setTimeout(() => setStatus(t("waking")), 12000);
    try {
      const res = await fetch(`${API}${path}?${params}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || "");
        throw new Error(res.status === 404 ? t("errNoImagery") : res.status === 422 ? t("errInput") + detail : t("errNetwork"));
      }
      state.data = { mode: state.mode, body: await res.json(), label: state.place.label };
      state.layer = "split";
      setStatus("");
      $("results").hidden = false;
      centreSplit();
      renderResults();
      updateLink(params);
    } catch (err) {
      setStatus(err instanceof TypeError ? t("errNetwork") : err.message, true);
    } finally {
      clearTimeout(wake);
      button.disabled = false;
    }
  });

  /* ------------------------------------------------------------------ drawing the cell grid */

  function hexToRgb(hex) {
    const h = hex.replace("#", "");
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }

  // One pixel per cell, then enlarged without smoothing so cells stay crisp on the map.
  function gridCanvas(groups, changed) {
    const rows = groups.length, cols = groups[0].length;
    const small = document.createElement("canvas");
    small.width = cols; small.height = rows;
    const ctx = small.getContext("2d");
    const img = ctx.createImageData(cols, rows);
    const colors = GROUPS.map((g) => hexToRgb(cssVar(`--c-${g}`)));
    const magenta = hexToRgb(cssVar("--c-changed"));
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const i = (r * cols + c) * 4;
        const rgb = changed ? (changed[r][c] ? magenta : [0, 0, 0]) : colors[groups[r][c]];
        img.data[i] = rgb[0]; img.data[i + 1] = rgb[1]; img.data[i + 2] = rgb[2];
        img.data[i + 3] = changed && !changed[r][c] ? 150 : 255;
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

  function paintSide(target, side, changed) {
    const overlay = gridCanvas(side.grid.groups, changed);
    setImage(target, "scene", side.image.src, side.image.corners, { "raster-fade-duration": 0 });
    setImage(target, "cells", overlay.toDataURL(), side.grid.corners,
      { "raster-opacity": state.opacity, "raster-resampling": "nearest", "raster-fade-duration": 0 });
    target.setPaintProperty("cells", "raster-opacity", state.opacity);
  }

  // In split view the main map holds the earlier date and the clipped map the later one.
  function showOnMap() {
    const { mode, body } = state.data;
    const split = mode === "change" && state.layer === "split";
    const side = mode === "now" ? body : split || state.layer === "before" ? body.before : body.after;
    const changeLayer = mode === "change" && state.layer === "change";
    paintSide(map, side, changeLayer ? body.changed : null);
    // Legend entries the current layer does not draw are dimmed.
    $("legend").querySelectorAll("li").forEach((li) =>
      li.classList.toggle("off", changeLayer !== (li.dataset.key === "changed")));
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
          <span class="delta">%${num(g.share)} &nbsp; ${num(g.km2)} km²</span></div>
        <div class="track"><i style="width:${g.share}%;background:var(--c-${g.key})"></i></div>
      </li>`).join("")}</ul>`;
  }

  function classTable(classes) {
    const rows = classes.filter((c) => c.share > 0).sort((a, b) => b.share - a.share)
      .map((c) => `<tr><td>${esc(t(c.key))}</td><td>%${num(c.share)}</td><td>${num(c.km2)} km²</td></tr>`).join("");
    return `<details><summary>${esc(t("details"))}</summary><table><tbody>${rows}</tbody></table></details>`;
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

  function renderNow(body) {
    return `
      <div><h2>${esc(state.data.label)}</h2>
        <p class="meta">${esc(t("imageDate"))}: ${esc(body.date)} &nbsp; ${esc(t("totalArea"))}: ${num(body.stats.total_km2, 0)} km²</p></div>
      ${notices(body)}
      ${bars(body.stats.groups)}
      ${classTable(body.stats.classes)}
      ${opacityControl()}
      ${footer()}`;
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
        <td>%${num(g.share)}</td><td>%${num(after.share)}</td>
        <td>${delta > 0 ? "+" : ""}${num(delta)}</td></tr>`;
    }).join("");
    const moves = body.transitions.slice(0, 5)
      .map((m) => `<li><span>${esc(t(m.from))} → ${esc(t(m.to))}</span><span>${num(m.km2)} km²</span></li>`).join("");
    const layerButton = (key) =>
      `<button type="button" data-layer="${key}" aria-selected="${state.layer === key}">${esc(t("layer" + key[0].toUpperCase() + key.slice(1)))}</button>`;

    return `
      <div><h2>${esc(state.data.label)}</h2>
        <p class="meta">${esc(a.date)} → ${esc(b.date)} &nbsp; ${esc(t("totalArea"))}: ${num(a.stats.total_km2, 0)} km²</p></div>
      <p class="figure"><strong>${num(body.built_km2)} km²</strong><span>${esc(t("builtGain"))}</span></p>
      <p class="meta">${esc(t("changedTotal"))}: ${num(body.changed_km2)} km²</p>
      ${notices(a, b)}
      <div><p class="subhead">${esc(t("layer"))}</p>
        <div class="segmented small" id="layer">${["split", "before", "after", "change"].map(layerButton).join("")}</div>
        <div class="sides" id="sides"${state.layer === "split" ? "" : " hidden"}>
          <p><span>${esc(t("splitLeft"))}</span><strong>${esc(t("before"))} · ${esc(a.date)}</strong></p>
          <p><span>${esc(t("splitRight"))}</span><strong>${esc(t("after"))} · ${esc(b.date)}</strong></p>
          <p class="hint">${esc(t("splitHint"))}</p>
        </div></div>
      ${opacityControl()}
      <table><thead><tr><th></th><th>${esc(a.date.slice(0, 4))}</th><th>${esc(b.date.slice(0, 4))}</th><th>±</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div><p class="subhead">${esc(t("transitions"))}</p>
        ${moves ? `<ul class="transitions">${moves}</ul>` : `<p class="meta">${esc(t("noChange"))}</p>`}</div>
      ${footer()}`;
  }

  function renderLegend(mode) {
    const legend = $("legend");
    const keys = mode === "change" ? [...GROUPS, "changed"] : GROUPS;
    legend.setAttribute("aria-label", t("legend"));
    legend.innerHTML = keys.map((k) => `<li data-key="${k}">${swatch(k)}${esc(t(k))}</li>`).join("");
    legend.hidden = false;
  }

  function renderResults() {
    const { mode, body } = state.data;
    const panel = $("results");
    panel.innerHTML = mode === "now" ? renderNow(body) : renderChange(body);
    renderLegend(mode);
    if (mode === "change") {
      $("split-tag-before").textContent = body.before.date;
      $("split-tag-after").textContent = body.after.date;
    }
    const side = showOnMap();
    fit(side.image.corners, 900);

    $("opacity").addEventListener("input", (e) => {
      state.opacity = Number(e.target.value) / 100;
      maps().forEach((target) => { if (target.getLayer("cells")) target.setPaintProperty("cells", "raster-opacity", state.opacity); });
    });
    $("again").addEventListener("click", () => { searchInput.value = ""; searchInput.focus(); });
    $("copy").addEventListener("click", async (e) => {
      try { await navigator.clipboard.writeText(location.href); e.target.textContent = t("copied"); } catch { /* clipboard blocked */ }
    });

    if (mode === "change") {
      $("layer").addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b) return;
        state.layer = b.dataset.layer;
        document.querySelectorAll("#layer button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
        $("sides").hidden = state.layer !== "split";
        showOnMap();
      });
    }
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
    setMode(p.get("mode") === "change" ? "change" : "now");
    ["when", "before", "after"].forEach((k) => { if (p.get(k)) $(k).value = p.get(k); });
    setPlace(lat, lon, p.get("q") || `${lat.toFixed(3)}, ${lon.toFixed(3)}`);
    $("controls").requestSubmit();
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
