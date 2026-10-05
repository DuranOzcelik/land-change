/* Land Change front end: globe, place search, API calls and result rendering. */
(() => {
  "use strict";

  const API = window.LAND_CHANGE_API;
  const LANGS = window.LANGS;
  const GROUPS = ["agriculture", "vegetation", "built", "water"];
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
    layer: "after",       // which date the map shows in change mode: before | after | change
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
  const map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      projection: { type: "globe" },
      sources: { sat: { type: "raster", tiles: [BASEMAP], tileSize: 256, maxzoom: 14 } },
      layers: [
        { id: "ocean", type: "background", paint: { "background-color": "#0b2942" } },
        { id: "sat", type: "raster", source: "sat" },
      ],
      sky: { "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 0, 1, 5, 1, 7, 0] },
    },
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

  map.on("load", () => {
    map.addSource("frame", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "frame", type: "line", source: "frame", paint: { "line-color": "#ffffff", "line-width": 2 } });
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

  let marker = null;

  function approxSquare(lat, lon, km) {
    const dLat = km / 2 / 110.574;
    const dLon = km / 2 / (111.32 * Math.cos((lat * Math.PI) / 180));
    return [[lon - dLon, lat + dLat], [lon + dLon, lat + dLat], [lon + dLon, lat - dLat], [lon - dLon, lat - dLat]];
  }

  function setFrame(corners) {
    const src = map.getSource("frame");
    if (!src) return;
    src.setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [...corners, corners[0]] } });
  }

  function fit(corners, duration = 1400) {
    const lons = corners.map((c) => c[0]);
    const lats = corners.map((c) => c[1]);
    map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
      { padding: padding(), duration: reducedMotion ? 0 : duration });
  }

  function setImage(id, url, coordinates, paint) {
    const src = map.getSource(id);
    if (src) { src.updateImage({ url, coordinates }); return; }
    map.addSource(id, { type: "image", url, coordinates });
    map.addLayer({ id, type: "raster", source: id, paint }, "frame");
  }

  /* ------------------------------------------------------------------ place selection */

  function setPlace(lat, lon, label, fly = true) {
    stopSpin();
    lat = Math.max(-60, Math.min(75, lat));
    state.place = { lat, lon, label };
    $("hero").hidden = true;
    $("options").hidden = false;
    if (!marker) {
      const el = document.createElement("div");
      el.className = "pin";
      marker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map);
    } else {
      marker.setLngLat([lon, lat]);
    }
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
  $("after").value = String(today.getMonth() >= 7 ? today.getFullYear() : today.getFullYear() - 1);

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
      state.layer = "after";
      setStatus("");
      $("results").hidden = false;
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

  function showOnMap() {
    const { mode, body } = state.data;
    const side = mode === "now" ? body : state.layer === "before" ? body.before : body.after;
    const overlay = mode === "change" && state.layer === "change"
      ? gridCanvas(side.grid.groups, body.changed)
      : gridCanvas(side.grid.groups, null);
    setImage("scene", side.image.src, side.image.corners, { "raster-fade-duration": 0 });
    setImage("cells", overlay.toDataURL(), side.grid.corners,
      { "raster-opacity": state.opacity, "raster-resampling": "nearest", "raster-fade-duration": 0 });
    map.setPaintProperty("cells", "raster-opacity", state.opacity);
    setFrame(side.image.corners);
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
      return `<tr><td><span class="name">${swatch(g.key)}${esc(t(g.key))}</span></td>
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
      <div><p class="subhead">${esc(t("slider"))}</p>
        <div class="compare" id="compare">
          <canvas id="cmp-a" width="640" height="640"></canvas>
          <canvas id="cmp-b" width="640" height="640"></canvas>
          <div class="handle" id="cmp-handle"></div>
          <span class="tag a">${esc(a.date.slice(0, 4))}</span><span class="tag b">${esc(b.date.slice(0, 4))}</span>
          <input id="cmp-range" type="range" min="0" max="100" value="50" aria-label="${esc(t("slider"))}">
        </div></div>
      <div><p class="subhead">${esc(t("layer"))}</p>
        <div class="segmented small" id="layer">${["before", "after", "change"].map(layerButton).join("")}</div></div>
      ${opacityControl()}
      <table><thead><tr><th></th><th>${esc(a.date.slice(0, 4))}</th><th>${esc(b.date.slice(0, 4))}</th><th>±</th></tr></thead>
        <tbody>${rows}</tbody></table>
      <div><p class="subhead">${esc(t("transitions"))}</p>
        ${moves ? `<ul class="transitions">${moves}</ul>` : `<p class="meta">${esc(t("noChange"))}</p>`}</div>
      ${footer()}`;
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = src;
    });
  }

  // Satellite image with the class grid blended on top, for the drag-to-compare widget.
  async function paintComparison(canvasId, side, cellM) {
    const canvas = $(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const photo = await loadImage(side.image.src);
    ctx.drawImage(photo, 0, 0, canvas.width, canvas.height);
    const k = canvas.width / photo.naturalWidth;
    const cellPx = cellM / 10;
    const inset = cellM === 320 ? 16 : 0;
    ctx.globalAlpha = 0.5;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(gridCanvas(side.grid.groups, null), inset * k, inset * k,
      side.grid.cols * cellPx * k, side.grid.rows * cellPx * k);
    ctx.globalAlpha = 1;
  }

  function moveComparison(value) {
    const top = $("cmp-b"), handle = $("cmp-handle");
    if (!top) return;
    top.style.clipPath = `inset(0 0 0 ${value}%)`;
    handle.style.left = `${value}%`;
  }

  function renderResults() {
    const { mode, body } = state.data;
    const panel = $("results");
    panel.innerHTML = mode === "now" ? renderNow(body) : renderChange(body);
    const side = showOnMap();
    fit(side.image.corners, 900);

    $("opacity").addEventListener("input", (e) => {
      state.opacity = Number(e.target.value) / 100;
      map.setPaintProperty("cells", "raster-opacity", state.opacity);
    });
    $("again").addEventListener("click", () => { searchInput.value = ""; searchInput.focus(); });
    $("copy").addEventListener("click", async (e) => {
      try { await navigator.clipboard.writeText(location.href); e.target.textContent = t("copied"); } catch { /* clipboard blocked */ }
    });

    if (mode === "change") {
      paintComparison("cmp-a", body.before, body.cell_m);
      paintComparison("cmp-b", body.after, body.cell_m);
      moveComparison(50);
      $("cmp-range").addEventListener("input", (e) => moveComparison(Number(e.target.value)));
      $("layer").addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b) return;
        state.layer = b.dataset.layer;
        document.querySelectorAll("#layer button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
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
