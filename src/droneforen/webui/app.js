/* Drone 4Ren web GUI - self-contained, no external assets. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = JSON.stringify((await res.json()).detail); } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

function notice(el, text, isError) {
  el.innerHTML = `<div class="notice${isError ? " error" : ""}">${esc(text)}</div>`;
}

/* ------------------------------------------------------------------ tabs */
const PAGE = { timeline: { offset: 0, limit: 50 } };
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    LOADERS[btn.dataset.tab]?.();
  });
});

/* ------------------------------------------------------------- dashboard */
async function loadDashboard() {
  const info = await api("/api/case");
  $("#case-number").textContent = info.meta.case_number;
  const m = info.meta;
  $("#case-meta").innerHTML = [
    ["Case id", `<span class="mono">${esc(m.case_id)}</span>`],
    ["Context", esc(m.context)],
    ["Operator label", esc(m.collector)],
    ["Created (UTC)", esc(m.created_utc)],
    ["Activity label", esc(info.actor)],
    ["Tool", `droneforen ${esc(info.tool_version)}`],
  ].map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("");

  const w = await api("/api/warnings");
  $("#warnings-list").innerHTML =
    w.pre_action_warnings.map((x) => `<li>${esc(x)}</li>`).join("");

  const ev = await api("/api/evidence");
  $("#evidence-table").innerHTML =
    "<tr><th>Item</th><th>SHA-256</th><th>Size</th><th>Acquisition</th>" +
    "<th>Origin</th><th>Runs</th><th></th></tr>" +
    ev.items.map((e) => `<tr>
      <td>${esc(e.original_name)}<br><span class="muted">${esc(e.description || "")}</span></td>
      <td class="mono">${esc(e.sha256.slice(0, 16))}...</td>
      <td>${e.size_bytes}</td><td>${esc(e.acquisition_method)}</td>
      <td>${esc(e.data_origin)}</td><td>${e.parser_run_count}</td>
      <td><button class="btn-parse" data-sha="${esc(e.sha256)}">parse</button></td>
    </tr>`).join("") || "<tr><td>no evidence yet</td></tr>";
  document.querySelectorAll(".btn-parse").forEach((btn) =>
    btn.addEventListener("click", () => doParse(btn.dataset.sha, btn)));
}

async function doParse(sha, btn) {
  btn.disabled = true;
  try {
    const out = await api("/api/parse", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sha_prefix: sha }),
    });
    notice($("#intake-result"),
      `parse ${out.status}: ${out.parser_name} ${out.parser_version}, ` +
      `${out.event_count} events${out.error ? " - " + out.error : ""}`,
      out.status !== "completed");
  } catch (err) { notice($("#intake-result"), err.message, true); }
  btn.disabled = false;
  loadDashboard();
}

$("#btn-verify").addEventListener("click", async () => {
  const el = $("#verify-result");
  try {
    const v = await api("/api/verify", { method: "POST" });
    el.innerHTML = `<div class="notice${v.ok ? "" : " error"}">
      <span class="${v.ok ? "ok" : "fail"}">${v.ok ? "VERIFY OK" : "VERIFY FAILED"}</span>
      - ${v.store.item_count} items rehashed, ${v.audit.record_count} audit records,
      chain head <span class="mono">${esc(v.audit.head_sha256.slice(0, 24))}...</span>
      ${v.problems.map((p) => `<br>• ${esc(p)}`).join("")}</div>`;
  } catch (err) { notice(el, err.message, true); }
});

$("#intake-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const el = $("#intake-result");
  if (!$("#ack").checked) {
    notice(el, "acknowledge the evidence-alteration warnings first", true);
    return;
  }
  const fd = new FormData();
  fd.append("file", $("#f-file").files[0]);
  fd.append("acknowledged", "true");
  fd.append("description", $("#f-desc").value);
  fd.append("acquisition_method", $("#f-acq").value);
  fd.append("data_origin", $("#f-origin").value);
  if ($("#f-device").value) fd.append("source_device", $("#f-device").value);
  if ($("#f-wbmake").value) fd.append("wb_make", $("#f-wbmake").value);
  if ($("#f-wbmodel").value) fd.append("wb_model", $("#f-wbmodel").value);
  if ($("#f-wbtest").value) fd.append("wb_test_result", $("#f-wbtest").value);
  try {
    const out = await api("/api/evidence", { method: "POST", body: fd });
    notice(el, `intaken ${out.original_name} (${out.size_bytes} bytes) ` +
      `sha256 ${out.sha256.slice(0, 16)}... - detected parsers: ` +
      `${out.detected_parsers.join(", ") || "none"}` +
      (out.write_blocker_documented ? "" : " - NOTE: no write blocker documented"));
    $("#intake-form").reset();
    loadDashboard();
  } catch (err) { notice(el, err.message, true); }
});

/* -------------------------------------------------------------- timeline */
async function loadTimeline() {
  const p = PAGE.timeline;
  const params = new URLSearchParams({ limit: p.limit, offset: p.offset });
  if ($("#tl-type").value) params.set("event_type", $("#tl-type").value);
  if ($("#tl-source").value) params.set("source", $("#tl-source").value);
  const data = await api(`/api/timeline?${params}`);
  const s = data.summary;
  $("#tl-summary").textContent =
    `${s.total_events} events (${s.anchored_events} UTC-anchored, ` +
    `${s.unanchored_events} unanchored)`;
  fillSelect($("#tl-type"),
    [...new Set(data.entries.map((e) => e.event_type))], "all event types");
  fillSelect($("#tl-source"),
    s.sources.map((x) => x.sha256.slice(0, 12)), "all sources");
  $("#tl-page").textContent = `${p.offset + 1}-${p.offset + data.entries.length}`;
  $("#timeline-table").innerHTML =
    "<tr><th>Time (UTC)</th><th>Event</th><th>Position</th><th>Payload</th>" +
    "<th>Source</th></tr>" +
    data.entries.map((e) => `<tr>
      <td>${esc(e.ts_utc || `(${e.ts_original || "no time"} ${e.clock_confidence || ""})`)}</td>
      <td>${esc(e.event_type)}</td>
      <td class="mono">${e.lat != null ? `${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}` : ""}</td>
      <td class="mono">${esc(JSON.stringify(e.payload).slice(0, 110))}</td>
      <td class="muted">${esc(e.evidence_name)}:${esc(e.source_offset || "-")}
        via ${esc(e.parser_name)}</td>
    </tr>`).join("");
}
function fillSelect(sel, values, allLabel) {
  const current = sel.value;
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    values.map((v) => `<option${v === current ? " selected" : ""}>${esc(v)}</option>`).join("");
}
$("#tl-type").addEventListener("change", () => { PAGE.timeline.offset = 0; loadTimeline(); });
$("#tl-source").addEventListener("change", () => { PAGE.timeline.offset = 0; loadTimeline(); });
$("#tl-prev").addEventListener("click", () => {
  PAGE.timeline.offset = Math.max(0, PAGE.timeline.offset - PAGE.timeline.limit);
  loadTimeline();
});
$("#tl-next").addEventListener("click", () => {
  PAGE.timeline.offset += PAGE.timeline.limit; loadTimeline();
});

/* ------------------------------------------------------------------- map */
const COLORS = ["#5aa2e6", "#e6796f", "#68c777", "#b98be0", "#e0b341"];
const MAP_W = 900, MAP_H = 540, MAP_PAD = 45;
const NICE_M = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
let MAP = null;  // { lines, pts, bounds } cached so offline/online toggles don't refetch

/* A round scale-bar distance whose pixel length is <= maxPx. */
function niceScale(metersPerPixel, maxPx) {
  let best = NICE_M[0];
  for (const m of NICE_M) { if (m / metersPerPixel <= maxPx) best = m; }
  const px = best / metersPerPixel;
  const label = best >= 1000 ? `${best / 1000} km` : `${best} m`;
  return { meters: best, px, label };
}
function scaleBarSvg(x, y, metersPerPixel) {
  const s = niceScale(metersPerPixel, 150);
  return `<g font-family="monospace" font-size="11" fill="#cdd6df">
    <rect x="${x}" y="${y}" width="${s.px.toFixed(1)}" height="6" fill="#cdd6df"
      stroke="#0c1116"/>
    <rect x="${x}" y="${y}" width="${(s.px / 2).toFixed(1)}" height="6" fill="#0c1116"
      stroke="#cdd6df"/>
    <text x="${x}" y="${y - 4}">${s.label}</text></g>`;
}
function northArrowSvg(x, y) {
  return `<g fill="#cdd6df" stroke="#0c1116" stroke-width=".5">
    <polygon points="${x},${y - 16} ${x - 5},${y} ${x},${y - 5} ${x + 5},${y}"/>
    <text x="${x - 4}" y="${y + 12}" font-family="monospace" font-size="11"
      fill="#cdd6df" stroke="none">N</text></g>`;
}

/* Web Mercator world-pixel projection (256px tiles). */
function mercXY(lon, lat, z) {
  const world = 256 * 2 ** z;
  const s = Math.min(Math.max(Math.sin(lat * Math.PI / 180), -0.9999), 0.9999);
  return [(lon + 180) / 360 * world,
          (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * world];
}

async function loadMap() {
  const gj = await api("/api/tracks");
  const lines = gj.features.filter((f) => f.geometry.type === "LineString");
  const pts = gj.features.filter((f) => f.geometry.type === "Point");
  const all = [];
  lines.forEach((l) => l.geometry.coordinates.forEach((c) => all.push(c)));
  pts.forEach((p) => all.push(p.geometry.coordinates));
  if (!all.length) {
    MAP = null;
    $("#map-holder").innerHTML = "<p class='muted'>no positions</p>";
    return;
  }
  const lons = all.map((c) => c[0]), lats = all.map((c) => c[1]);
  MAP = { lines, pts, bounds: {
    lon0: Math.min(...lons), lon1: Math.max(...lons),
    lat0: Math.min(...lats), lat1: Math.max(...lats) } };
  // always (re)enter the Map tab in the offline default, never mid-online-state
  $("#map-offline").classList.add("active");
  $("#map-online").classList.remove("active");
  $("#map-warning").innerHTML = "";
  $("#map-mode-note").textContent = "Offline map · no tile requests";
  $("#map-caption").textContent =
    "Local plot of parsed positions with scale bar and north arrow. " +
    "Choose Load street basemap (online) to display streets.";
  renderSchematic();
}

/* Default view: offline equirectangular schematic with scale + north + grid. */
function renderSchematic() {
  if (!MAP) return;
  const { lines, pts } = MAP;
  const { lon0, lon1, lat0, lat1 } = MAP.bounds;
  const W = MAP_W, H = MAP_H, PAD = MAP_PAD;
  const cos = Math.cos(((lat0 + lat1) / 2) * Math.PI / 180) || 1e-9;
  const scale = Math.min((W - 2 * PAD) / Math.max((lon1 - lon0) * cos, 1e-7),
                         (H - 2 * PAD) / Math.max(lat1 - lat0, 1e-7));
  const xy = (c) => [PAD + (c[0] - lon0) * cos * scale,
                     H - PAD - (c[1] - lat0) * scale];
  const metersPerPixel = 110574 / scale;  // 1 deg latitude ~= 110.574 km

  let svg = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  // corner coordinate labels
  svg += `<text x="${PAD}" y="18" font-size="11" fill="#93a0ad" font-family="monospace">` +
         `NE ${lat1.toFixed(6)}, ${lon1.toFixed(6)}</text>`;
  svg += `<text x="${PAD}" y="${H - 22}" font-size="11" fill="#93a0ad" font-family="monospace">` +
         `SW ${lat0.toFixed(6)}, ${lon0.toFixed(6)}</text>`;
  // faint reference grid (thirds of the extent) with edge lat/lon labels
  for (let k = 1; k <= 2; k++) {
    const gx = PAD + (W - 2 * PAD) * k / 3, gy = PAD + (H - 2 * PAD) * k / 3;
    svg += `<line x1="${gx}" y1="${PAD}" x2="${gx}" y2="${H - PAD}" stroke="#243039"/>`;
    svg += `<line x1="${PAD}" y1="${gy}" x2="${W - PAD}" y2="${gy}" stroke="#243039"/>`;
    const lonAt = lon0 + (lon1 - lon0) * k / 3;
    const latAt = lat1 - (lat1 - lat0) * k / 3;
    svg += `<text x="${gx + 2}" y="${H - PAD - 3}" font-size="9" fill="#5f6b77"
      font-family="monospace">${lonAt.toFixed(5)}</text>`;
    svg += `<text x="${PAD + 2}" y="${gy - 3}" font-size="9" fill="#5f6b77"
      font-family="monospace">${latAt.toFixed(5)}</text>`;
  }
  svg += drawTracksSvg(lines, pts, xy);
  svg += `<text x="8" y="14" font-size="10" fill="#93a0ad">start: ○ end: ■ media: ●</text>`;
  svg += northArrowSvg(W - 24, 40);
  svg += scaleBarSvg(PAD, H - PAD + 22, metersPerPixel);
  svg += `</svg>`;
  $("#map-holder").innerHTML = svg;
}

/* Shared track/marker drawing given a projection function c -> [x,y]. */
function drawTracksSvg(lines, pts, xy) {
  let svg = "", ly = 36;
  const W = MAP_W, PAD = MAP_PAD;
  lines.forEach((l, i) => {
    const color = COLORS[i % COLORS.length];
    const path = l.geometry.coordinates
      .map((c) => xy(c).map((v) => v.toFixed(1)).join(",")).join(" ");
    svg += `<polyline points="${path}" fill="none" stroke="${color}" stroke-width="2.5"
      stroke-opacity=".95"><title>${esc(l.properties.evidence_name)}</title></polyline>`;
    const [sx, sy] = xy(l.geometry.coordinates[0]);
    svg += `<circle cx="${sx}" cy="${sy}" r="5" fill="none" stroke="${color}" stroke-width="2"/>`;
    const [ex, ey] = xy(l.geometry.coordinates.at(-1));
    svg += `<rect x="${ex - 4}" y="${ey - 4}" width="8" height="8" fill="${color}"/>`;
    svg += `<text x="${W - PAD - 360}" y="${ly}" font-size="11" fill="${color}"
      font-family="monospace">- ${esc(l.properties.evidence_name.slice(0, 56))}</text>`;
    ly += 15;
  });
  pts.forEach((p) => {
    const [x, y] = xy(p.geometry.coordinates);
    svg += `<circle cx="${x}" cy="${y}" r="5" fill="#f0b000" stroke="#0c1116"
      stroke-width="1"><title>media: ${esc(p.properties.evidence_name)}
      ${esc(p.properties.ts_utc || "")}</title></circle>`;
  });
  return svg;
}

/* Fetch map tiles only after the user confirms. */
function requestOnlineBasemap() {
  if (!MAP) { renderSchematic(); return; }
  const { lon0, lon1, lat0, lat1 } = MAP.bounds;
  $("#map-warning").innerHTML = `<div class="warnbox">
    <strong>Go online to load a street basemap?</strong>
    This contacts <span class="mono">tile.openstreetmap.org</span> over the
    internet and discloses this case's coordinates
    (<span class="mono">${lat0.toFixed(4)},${lon0.toFixed(4)}</span> to
    <span class="mono">${lat1.toFixed(4)},${lon1.toFixed(4)}</span>) to a
    third-party server. Do not use this on air-gapped evidence or where the
    location is sensitive. Track data itself is never uploaded - only the map
    tiles for this area are fetched.
    <div class="row" style="margin-top:.5rem">
      <button id="map-go-online">Yes, go online</button>
      <button id="map-cancel-online" style="background:var(--line);
        border-color:var(--line)">Cancel</button>
    </div></div>`;
  $("#map-go-online").addEventListener("click", () => {
    $("#map-warning").innerHTML = "";
    renderOnlineMap();
  });
  $("#map-cancel-online").addEventListener("click", () => {
    $("#map-warning").innerHTML = "";
    $("#map-offline").classList.add("active");
    $("#map-online").classList.remove("active");
  });
}

function renderOnlineMap() {
  const { lines, pts } = MAP;
  const { lon0, lon1, lat0, lat1 } = MAP.bounds;
  const W = MAP_W, H = MAP_H, PAD = MAP_PAD;
  // pick the highest zoom whose bbox still fits the viewport
  let z = 19;
  for (; z > 1; z--) {
    const [x0] = mercXY(lon0, lat1, z), [x1] = mercXY(lon1, lat1, z);
    const [, y0] = mercXY(lon0, lat1, z), [, y1] = mercXY(lon0, lat0, z);
    if ((x1 - x0) <= W - 2 * PAD && (y1 - y0) <= H - 2 * PAD) break;
  }
  const [cx, cy] = mercXY((lon0 + lon1) / 2, (lat0 + lat1) / 2, z);
  const originX = cx - W / 2, originY = cy - H / 2;      // world px at viewport TL
  const proj = (c) => { const [x, y] = mercXY(c[0], c[1], z);
    return [x - originX, y - originY]; };

  const maxTile = 2 ** z;
  let tiles = "";
  const tx0 = Math.floor(originX / 256), tx1 = Math.floor((originX + W) / 256);
  const ty0 = Math.floor(originY / 256), ty1 = Math.floor((originY + H) / 256);
  for (let tx = tx0; tx <= tx1; tx++) {
    for (let ty = ty0; ty <= ty1; ty++) {
      if (tx < 0 || ty < 0 || tx >= maxTile || ty >= maxTile) continue;
      const left = tx * 256 - originX, top = ty * 256 - originY;
      tiles += `<img src="https://tile.openstreetmap.org/${z}/${tx}/${ty}.png"
        data-map-tile
        style="position:absolute;left:${left}px;top:${top}px;width:256px;height:256px"
        loading="eager" alt="">`;
    }
  }
  const metersPerPixel = 156543.03392 *
    Math.cos((lat0 + lat1) / 2 * Math.PI / 180) / 2 ** z;
  let overlay = `<svg class="map-track-overlay" viewBox="0 0 ${W} ${H}"
    width="${W}" height="${H}"
    style="position:absolute;left:0;top:0" xmlns="http://www.w3.org/2000/svg">`;
  overlay += drawTracksSvg(lines, pts, proj);
  overlay += northArrowSvg(W - 24, 40);
  overlay += scaleBarSvg(PAD, H - 24, metersPerPixel);
  overlay += `</svg>`;

  $("#map-holder").innerHTML =
    `<div style="position:relative;width:${W}px;max-width:100%;height:${H}px;
       overflow:hidden;background:#0c1116;border:1px solid var(--line);
       border-radius:4px">
       <div style="position:absolute;inset:0">${tiles}</div>
       ${overlay}
       <div id="map-tile-status" role="status" style="position:absolute;left:6px;bottom:4px;
         font-size:11px;background:rgba(12,17,22,.82);color:#cdd6df;padding:2px 6px;
         border-radius:3px">Loading street tiles...</div>
       <div style="position:absolute;right:4px;bottom:2px;font-size:10px;
         background:rgba(12,17,22,.7);color:#cdd6df;padding:1px 5px;border-radius:3px">
         © OpenStreetMap contributors · zoom ${z}</div>
     </div>`;
  const tileImages = [...document.querySelectorAll("#map-holder img[data-map-tile]")];
  const tileStatus = $("#map-tile-status");
  let loaded = 0, failed = 0;
  const updateTileStatus = () => {
    if (!tileStatus) return;
    const finished = loaded + failed;
    if (finished < tileImages.length) {
      tileStatus.textContent = `Loading street tiles... ${finished}/${tileImages.length}`;
    } else if (loaded === 0) {
      tileStatus.textContent =
        "Street tiles could not load. Check internet access, then try again.";
      tileStatus.style.color = "var(--bad)";
    } else if (failed > 0) {
      tileStatus.textContent = `Street map loaded with ${failed} missing tile(s).`;
      tileStatus.style.color = "var(--warn)";
    } else {
      tileStatus.textContent = "Street map loaded";
    }
  };
  tileImages.forEach((img) => {
    const markLoaded = () => { loaded += 1; updateTileStatus(); };
    const markFailed = () => { failed += 1; updateTileStatus(); };
    if (img.complete) {
      if (img.naturalWidth > 0) markLoaded(); else markFailed();
    } else {
      img.addEventListener("load", markLoaded, { once: true });
      img.addEventListener("error", markFailed, { once: true });
    }
  });
  updateTileStatus();
  $("#map-mode-note").innerHTML =
    "<span style='color:var(--warn)'>Online</span> · tiles from OpenStreetMap";
  $("#map-caption").textContent =
    "Street basemap from OpenStreetMap (fetched live). Tracks reprojected to " +
    "Web Mercator to align with the map. Switch back to the offline schematic " +
    "to stop contacting the network.";
}

$("#map-offline").addEventListener("click", () => {
  $("#map-offline").classList.add("active");
  $("#map-online").classList.remove("active");
  $("#map-warning").innerHTML = "";
  $("#map-mode-note").textContent = "Offline map · no tile requests";
  $("#map-caption").textContent =
    "mapping application.";
    "Local plot of parsed positions with scale bar and north arrow. Export " +
    "KML/GPX/GeoJSON below for GIS overlay in a mapping application.";
    "mapping tool.";
  renderSchematic();
});
$("#map-online").addEventListener("click", () => {
  $("#map-online").classList.add("active");
  $("#map-offline").classList.remove("active");
  requestOnlineBasemap();
});

document.querySelectorAll(".btn-export").forEach((btn) =>
  btn.addEventListener("click", async () => {
    try {
      const out = await api("/api/export", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format: btn.dataset.fmt }),
      });
      $("#export-result").innerHTML = `<div class="notice">wrote
        <a href="/files/${esc(out.name)}" download>${esc(out.name)}</a>
        - sha256 <span class="mono">${esc(out.sha256.slice(0, 24))}...</span></div>`;
    } catch (err) { notice($("#export-result"), err.message, true); }
  }));

/* ---------------------------------------------------------------- flight */
async function loadFlight() {
  const data = await api("/api/flights");
  $("#flights").innerHTML = data.flights.map((f) => `
    <h3 class="muted">${esc(f.evidence_name)} (${esc(f.evidence_sha256.slice(0, 12))},
      ${esc(f.parser)})</h3>
    <table>
      <tr><th>First / last fix</th><td>${esc(f.first_fix_utc || "-")} /
        ${esc(f.last_fix_utc || "-")}</td>
        <th>Duration / path</th><td>${f.duration_s ?? "-"} s /
        ${f.path_distance_m ?? "-"} m</td></tr>
      <tr><th>Max altitude</th><td>${f.max_alt_agl_m != null ? f.max_alt_agl_m + " m AGL"
        : f.max_alt_msl_m != null ? f.max_alt_msl_m + " m MSL" : "-"}</td>
        <th>Max speed / fixes</th><td>${f.max_speed_ms ?? "-"} m/s /
        ${f.position_count}</td></tr>
    </table>
    <table><tr><th>Derived item</th><th>Time (UTC)</th><th>Detail</th><th>Method</th></tr>
      ${f.items.map((i) => `<tr><td>${esc(i.kind)}</td><td>${esc(i.ts_utc || "-")}</td>
        <td>${esc(i.description)}</td><td class="muted">${esc(i.method)}</td></tr>`).join("")}
    </table>`).join("") || "<p class='muted'>no flight telemetry sources</p>";

  const c = await api("/api/correlations");
  $("#correlations-table").innerHTML =
    "<tr><th>Media</th><th>Capture (UTC)</th><th>Correlated</th>" +
    "<th>Track position</th><th>Offset</th><th>Notes</th></tr>" +
    c.correlations.map((x) => `<tr>
      <td>${esc(x.media_name)}</td><td>${esc(x.ts_utc || "-")}</td>
      <td>${x.correlated ? "yes" : "no"}</td>
      <td class="mono">${x.correlated ? `${x.track_lat}, ${x.track_lon}` : "-"}</td>
      <td>${x.offset_m != null ? x.offset_m + " m" : "-"}</td>
      <td class="muted">${esc(x.reason || "")}</td></tr>`).join("") ||
    "<tr><td>no media capture events</td></tr>";
}

/* ---------------------------------------------------------------- checks */
async function loadChecks() {
  const data = await api("/api/checks");
  $("#checks-table").innerHTML =
    "<tr><th>Severity</th><th>Check</th><th>Description</th></tr>" +
    data.findings.map((f) => `<tr>
      <td class="sev-${esc(f.severity)}">${esc(f.severity.toUpperCase())}</td>
      <td>${esc(f.check)}</td><td>${esc(f.description)}</td></tr>`).join("") ||
    "<tr><td colspan=3>no findings (absence of findings is not proof of " +
    "authenticity)</td></tr>";
}

/* ----------------------------------------------------------------- links */
async function loadLinks() {
  const g = await api("/api/entities");
  $("#entities-caveat").textContent = g.caveat;
  $("#entities-table").innerHTML =
    "<tr><th>Attribution</th><th>Type</th><th>Entity</th><th>Obs</th>" +
    "<th>Evidence</th><th>Note</th></tr>" +
    g.nodes.map((n) => `<tr>
      <td>${esc(n.attribution_level)}</td><td>${esc(n.type)}</td>
      <td class="mono">${esc(n.label)}</td><td>${n.observations}</td>
      <td class="muted">${esc(Object.values(n.evidence).join(", "))}</td>
      <td class="muted">${esc(n.note || "")}</td></tr>`).join("") ||
    "<tr><td>no entities extracted</td></tr>";
  $("#edges-table").innerHTML =
    "<tr><th>Relation</th><th>A</th><th>B</th><th>Detail</th></tr>" +
    g.edges.map((e) => `<tr><td>${esc(e.relation)}</td>
      <td class="mono">${esc(e.a)}</td><td class="mono">${esc(e.b)}</td>
      <td class="muted">${esc(e.detail || "")}</td></tr>`).join("") ||
    "<tr><td>no links</td></tr>";

  const s = await api("/api/scenarios");
  $("#scenario-buttons").innerHTML = s.scenarios.map((x) =>
    `<button class="btn-scenario" data-name="${esc(x.scenario)}">
       ${esc(x.scenario)}</button>`).join("");
  document.querySelectorAll(".btn-scenario").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const r = await api(`/api/scenarios/${btn.dataset.name}`);
      $("#scenario-result").innerHTML = `
        <h3 class="muted">${esc(r.scenario)}: ${esc(r.description)}</h3>
        <p class="warnbox">${esc(r.caveat)}</p>
        <table><tr><th>Status</th><th>Signal</th><th>Detail</th></tr>
        ${r.signals.map((x) => `<tr>
          <td class="${x.status === "present" ? "sev-warning"
                     : x.status === "absent" ? "ok" : "muted"}">
            ${esc(x.status.toUpperCase())}</td>
          <td>${esc(x.name)}</td><td class="muted">${esc(x.detail)}</td>
        </tr>`).join("")}</table>`;
    }));
}

/* --------------------------------------------------------------- reports */
async function loadReports() {
  const data = await api("/api/reports");
  $("#reports-table").innerHTML =
    "<tr><th>File</th><th>Size</th></tr>" +
    data.reports.map((r) => `<tr>
      <td><a href="/files/${esc(r.name)}" target="_blank">${esc(r.name)}</a></td>
      <td>${r.size_bytes}</td></tr>`).join("") || "<tr><td>no reports yet</td></tr>";
}
async function makeReport(format) {
  const el = $("#report-result");
  try {
    const out = await api("/api/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format }),
    });
    notice(el, `generated ${out.name} - sha256 ${out.sha256.slice(0, 24)}...`);
    loadReports();
  } catch (err) { notice(el, err.message, true); }
}
$("#btn-report-html").addEventListener("click", () => makeReport("html"));
$("#btn-report-pdf").addEventListener("click", () => makeReport("pdf"));

/* ----------------------------------------------------------------- audit */
async function loadAudit() {
  const data = await api("/api/audit?limit=200");
  $("#audit-total").textContent = `${data.total} records (showing last ${data.records.length})`;
  $("#audit-table").innerHTML =
    "<tr><th>#</th><th>Time (UTC)</th><th>Actor</th><th>Action</th>" +
    "<th>Target</th><th>Params</th></tr>" +
    data.records.slice().reverse().map((r) => `<tr>
      <td>${r.seq}</td><td>${esc(r.ts_utc)}</td><td>${esc(r.actor)}</td>
      <td>${esc(r.action)}</td><td class="mono">${esc(String(r.target).slice(0, 24))}</td>
      <td class="mono">${esc(JSON.stringify(r.params).slice(0, 100))}</td></tr>`).join("");
}

const LOADERS = {
  dashboard: loadDashboard, timeline: loadTimeline, map: loadMap,
  flight: loadFlight, checks: loadChecks, links: loadLinks,
  reports: loadReports, audit: loadAudit,
};
loadDashboard();
