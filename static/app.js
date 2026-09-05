"use strict";

const VESSELS = [
  { mmsi: 311486000, label: "Sicily Strait", color: "#34d399" },
  { mmsi: 247039300, label: "Adriatic — looks like two vessels sharing one MMSI", color: "#fbbf24" },
  { mmsi: 311040700, label: "Eastern Med — busiest", color: "#f472b6" },
];

// Datetime-local values carry no timezone, which matches the API: a naive bound is
// read as UTC. The full range ends one minute past the last real report so the
// half-open reported_to does not clip it.
const FULL_RANGE = { from: "2013-06-30T23:34", to: "2013-07-01T17:45" };
const BUSY_MINUTE = { from: "2013-07-01T17:43", to: "2013-07-01T17:44" };

// Every automatic zoom is capped: a result set that is one point, or a hundred points
// a mile apart, should still leave enough coastline on screen to say where it is.
const FIT = { maxZoom: 11 };

const state = {
  lastItems: [],
  nextCursor: null,
  radiusCentre: null,
  radiusCircleLayer: null,
  overviewLayers: [],
  queryLayerGroup: null,
  trackLayer: null,
  trackPoints: [],
};

let map;

function colorForMmsi(mmsi) {
  const vessel = VESSELS.find((v) => String(v.mmsi) === String(mmsi));
  return vessel ? vessel.color : "#38bdf8";
}

function getSelectedVessels() {
  return Array.from(document.querySelectorAll(".vessel-checkbox:checked")).map((cb) => cb.value);
}

function getSpatialMode() {
  return document.querySelector('input[name="spatial-mode"]:checked').value;
}

function appendIfSet(params, key, elementId) {
  const value = document.getElementById(elementId).value;
  if (value !== "") params.set(key, value);
}

function buildParams({ after } = {}) {
  const params = new URLSearchParams();
  getSelectedVessels().forEach((mmsi) => params.append("mmsi", mmsi));

  const from = document.getElementById("reported-from").value;
  if (from) params.set("reported_from", from);
  const to = document.getElementById("reported-to").value;
  if (to) params.set("reported_to", to);

  const mode = getSpatialMode();
  if (mode === "radius" && state.radiusCentre) {
    params.set("centre_latitude", state.radiusCentre.lat.toFixed(5));
    params.set("centre_longitude", state.radiusCentre.lon.toFixed(5));
    params.set("radius_nautical_miles", document.getElementById("radius-slider").value);
  } else if (mode === "bbox") {
    appendIfSet(params, "min_latitude", "min-lat");
    appendIfSet(params, "max_latitude", "max-lat");
    appendIfSet(params, "min_longitude", "min-lon");
    appendIfSet(params, "max_longitude", "max-lon");
  }

  const limit = document.getElementById("limit").value;
  if (limit) params.set("limit", limit);
  if (after) params.set("after", after);

  return params;
}

function updateTelemetry({ status, duration, headers, ok }) {
  const statusEl = document.getElementById("tele-status");
  statusEl.textContent = `status ${status}`;
  statusEl.className = "tele-item " + (ok ? "tele-ok" : "tele-error");
  document.getElementById("tele-duration").textContent = `${duration.toFixed(0)} ms`;

  if (headers) {
    const limit = headers.get("ratelimit-limit");
    const remaining = headers.get("ratelimit-remaining");
    const reset = headers.get("ratelimit-reset") ?? headers.get("retry-after");
    if (limit !== null) {
      document.getElementById("tele-limit").textContent = `rate limit ${remaining ?? "?"}/${limit}`;
    }
    if (reset !== null) {
      document.getElementById("tele-reset").textContent = `resets in ${reset}s`;
    }
  }
}

async function callApi(params, { accept = "application/json" } = {}) {
  const url = "/v1/position-reports?" + params.toString();
  const started = performance.now();
  let response;
  try {
    response = await fetch(url, { headers: { Accept: accept } });
  } catch (networkError) {
    updateTelemetry({ status: "network error", duration: performance.now() - started, ok: false });
    throw networkError;
  }
  updateTelemetry({
    status: response.status,
    duration: performance.now() - started,
    headers: response.headers,
    ok: response.ok,
  });
  return response;
}

function hideProblem() {
  document.getElementById("problem-panel").hidden = true;
}

function showProblem(doc, rawText) {
  document.getElementById("problem-panel").hidden = false;
  document.getElementById("problem-title").textContent = `${doc.status ?? ""} — ${doc.title ?? "Error"}`;
  document.getElementById("problem-detail").textContent = doc.detail || "";

  const list = document.getElementById("problem-errors");
  list.innerHTML = "";
  (doc.errors || []).forEach((error) => {
    const li = document.createElement("li");
    li.textContent = error.parameter ? `${error.parameter}: ${error.detail}` : error.detail;
    list.appendChild(li);
  });

  document.getElementById("problem-raw").textContent = rawText;
}

async function showProblemFromResponse(response) {
  const text = await response.text();
  try {
    showProblem(JSON.parse(text), text);
  } catch {
    showProblem({ status: response.status, title: "Unexpected response" }, text);
  }
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text ?? "";
  return td;
}

function renderTable(items, { append }) {
  const tbody = document.getElementById("results-body");
  if (!append) tbody.innerHTML = "";
  items.forEach((item) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(item.report_id),
      cell(item.mmsi),
      cell(item.reported_at),
      cell(item.nav_status),
      cell(item.speed_knots),
      cell(item.course_degrees),
      cell(item.heading_degrees),
      cell(item.rate_of_turn),
      cell(item.latitude),
      cell(item.longitude),
    );
    tbody.appendChild(tr);
  });
}

// Dimmed once a real search runs, so a result dot at the same point as the overview
// track of the same vessel (same color, same coordinate) is not drawn invisibly on
// top of an identically-colored line.
function dimOverview() {
  state.overviewLayers.forEach((layer) => layer.setStyle({ opacity: 0.15 }));
}

function renderQueryPoints(items, { append }) {
  if (!append) dimOverview();
  if (!state.queryLayerGroup) state.queryLayerGroup = L.layerGroup().addTo(map);
  if (!append) state.queryLayerGroup.clearLayers();

  items.forEach((item) => {
    const color = colorForMmsi(item.mmsi);
    const marker = L.circleMarker([item.latitude, item.longitude], {
      radius: 5,
      color: "#0b1220",
      weight: 1.5,
      fillColor: color,
      fillOpacity: 1,
    });
    marker.bindPopup(
      `<strong>${item.mmsi}</strong><br>Report ${item.report_id} · ${item.reported_at}` +
        `<br>${item.speed_knots ?? "—"} kn, course ${item.course_degrees ?? "—"}°`,
    );
    marker.addTo(state.queryLayerGroup);
  });

  if (!append && items.length) {
    const bounds = L.latLngBounds(items.map((i) => [i.latitude, i.longitude]));
    map.fitBounds(bounds.pad(0.2), FIT);
  }
}

async function runSearch(reset) {
  hideProblem();
  const after = reset ? undefined : state.nextCursor;
  const params = buildParams({ after });

  let response;
  try {
    response = await callApi(params);
  } catch {
    return;
  }

  if (!response.ok) {
    await showProblemFromResponse(response);
    return;
  }

  const doc = await response.json();
  state.lastItems = reset ? doc.items : state.lastItems.concat(doc.items);
  state.nextCursor = doc.next_cursor;

  renderTable(doc.items, { append: !reset });
  renderQueryPoints(doc.items, { append: !reset });

  const loadMore = document.getElementById("load-more");
  loadMore.hidden = state.nextCursor === null;
  loadMore.disabled = state.nextCursor === null;
  document.getElementById("results-empty").hidden = state.lastItems.length > 0;
}

async function downloadCsv() {
  hideProblem();
  const params = buildParams({});

  let response;
  try {
    response = await callApi(params, { accept: "text/csv" });
  } catch {
    return;
  }

  if (!response.ok) {
    await showProblemFromResponse(response);
    return;
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "position-reports.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function loadOverview() {
  const allBounds = [];
  for (const vessel of VESSELS) {
    const params = new URLSearchParams({ mmsi: String(vessel.mmsi), limit: "1000" });
    let response;
    try {
      response = await callApi(params);
    } catch {
      continue;
    }
    if (!response.ok) continue;

    const doc = await response.json();
    const latlngs = doc.items.map((i) => [i.latitude, i.longitude]);
    const line = L.polyline(latlngs, { color: vessel.color, weight: 2, opacity: 0.75 }).addTo(map);
    state.overviewLayers.push(line);
    allBounds.push(...latlngs);
  }
  if (allBounds.length) map.fitBounds(L.latLngBounds(allBounds).pad(0.1), FIT);
}

function updateRadiusCircle() {
  if (!state.radiusCentre) return;
  const nm = Number(document.getElementById("radius-slider").value);
  const meters = nm * 1852;
  if (state.radiusCircleLayer) map.removeLayer(state.radiusCircleLayer);
  state.radiusCircleLayer = L.circle([state.radiusCentre.lat, state.radiusCentre.lon], {
    radius: meters,
    color: "#38bdf8",
    weight: 2,
    fillOpacity: 0.08,
  }).addTo(map);
  document.getElementById("radius-centre").textContent =
    `Centre: ${state.radiusCentre.lat.toFixed(3)}, ${state.radiusCentre.lon.toFixed(3)}`;
}

function drawTrack(order) {
  if (!state.trackPoints.length) return;
  const points =
    order === "reported_at"
      ? [...state.trackPoints].sort((a, b) => a.reported_at.localeCompare(b.reported_at))
      : state.trackPoints;

  const trackSelect = document.getElementById("track-vessel");
  const color = colorForMmsi(trackSelect.value);
  const latlngs = points.map((p) => [p.latitude, p.longitude]);

  if (state.trackLayer) map.removeLayer(state.trackLayer);
  state.trackLayer = L.polyline(latlngs, {
    color,
    weight: 3,
    opacity: 0.95,
    dashArray: order === "reported_at" ? "2 6" : null,
  }).addTo(map);
  map.fitBounds(state.trackLayer.getBounds().pad(0.2), FIT);
}

async function loadFullTrack() {
  hideProblem();
  const trackSelect = document.getElementById("track-vessel");
  const params = new URLSearchParams({ mmsi: trackSelect.value, limit: "1000" });

  let response;
  try {
    response = await callApi(params);
  } catch {
    return;
  }
  if (!response.ok) {
    await showProblemFromResponse(response);
    return;
  }

  dimOverview();
  const doc = await response.json();
  state.trackPoints = doc.items;
  document.getElementById("track-order-controls").hidden = false;
  drawTrack(document.querySelector('input[name="track-order"]:checked').value);
}

async function sendInvalidQuery() {
  hideProblem();
  const params = new URLSearchParams({ radius_nautical_miles: "60" });
  let response;
  try {
    response = await callApi(params);
  } catch {
    return;
  }
  await showProblemFromResponse(response);
}

async function simulateRateLimit() {
  hideProblem();
  const button = document.getElementById("trigger-rate-limit");
  button.disabled = true;
  let last;
  for (let i = 0; i < 11; i++) {
    const params = new URLSearchParams({ limit: "1" });
    try {
      last = await callApi(params);
    } catch {
      break;
    }
    if (!last.ok) break;
  }
  if (last && !last.ok) await showProblemFromResponse(last);
  button.disabled = false;
}

function buildVesselList() {
  const list = document.getElementById("vessel-list");
  VESSELS.forEach((vessel) => {
    const label = document.createElement("label");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(vessel.mmsi);
    checkbox.checked = true;
    checkbox.className = "vessel-checkbox";

    const swatch = document.createElement("span");
    swatch.className = "vessel-swatch";
    swatch.style.background = vessel.color;

    label.append(checkbox, swatch, document.createTextNode(`${vessel.mmsi} — ${vessel.label}`));
    list.appendChild(label);
  });
}

function buildTrackVesselSelect() {
  const select = document.getElementById("track-vessel");
  VESSELS.forEach((vessel) => {
    const option = document.createElement("option");
    option.value = String(vessel.mmsi);
    option.textContent = `${vessel.mmsi} — ${vessel.label}`;
    select.appendChild(option);
  });
  // 311486000 has a clean 0.5 km (Report ID) vs. 7.3 km (Reported Time) median hop —
  // the dramatic before/after the hint text promises. 247039300 is noisy in *both*
  // orderings (its own anomaly, not a reordering effect), so it stays selectable but
  // is not the default.
  select.value = "311486000";
}

function wireControls() {
  document.querySelectorAll('input[name="spatial-mode"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      const mode = getSpatialMode();
      document.getElementById("radius-controls").hidden = mode !== "radius";
      document.getElementById("bbox-controls").hidden = mode !== "bbox";
      map.getContainer().style.cursor = mode === "radius" ? "crosshair" : "";
    }),
  );

  document.getElementById("radius-slider").addEventListener("input", (e) => {
    document.getElementById("radius-value").textContent = e.target.value;
    updateRadiusCircle();
  });

  map.on("click", (e) => {
    if (getSpatialMode() !== "radius") return;
    state.radiusCentre = { lat: e.latlng.lat, lon: e.latlng.lng };
    updateRadiusCircle();
  });

  document.querySelectorAll(".chip[data-preset]").forEach((button) =>
    button.addEventListener("click", () => {
      const preset = button.dataset.preset === "full" ? FULL_RANGE : BUSY_MINUTE;
      document.getElementById("reported-from").value = preset.from;
      document.getElementById("reported-to").value = preset.to;
    }),
  );

  document.getElementById("run-query").addEventListener("click", () => runSearch(true));
  document.getElementById("load-more").addEventListener("click", () => runSearch(false));
  document.getElementById("download-csv").addEventListener("click", downloadCsv);
  document.getElementById("load-track").addEventListener("click", loadFullTrack);
  document.querySelectorAll('input[name="track-order"]').forEach((radio) =>
    radio.addEventListener("change", (e) => drawTrack(e.target.value)),
  );
  document.getElementById("trigger-error").addEventListener("click", sendInvalidQuery);
  document.getElementById("trigger-rate-limit").addEventListener("click", simulateRateLimit);
  document.getElementById("problem-close").addEventListener("click", hideProblem);
}

function init() {
  // minZoom and maxBounds together stop the view ever reaching the zoom levels where
  // the world is narrower than the pane and Leaflet tiles repeated copies of it
  // sideways; noWrap on the layer below refuses to fetch those copies at all.
  map = L.map("map", {
    preferCanvas: true,
    minZoom: 3,
    maxBounds: L.latLngBounds([-85, -180], [85, 180]),
    maxBoundsViscosity: 0.75,
  }).setView([38.9, 23.2], 5);
  // Standard OSM tiles worked in development but started returning their "Access
  // denied" placeholder under repeated automated testing (see
  // https://operations.osmfoundation.org/policies/tiles/ — bulk/scripted use isn't
  // what the free tile server is for). Esri's ArcGIS Online basemap has no such
  // policy for this volume and needs no API key, unlike CARTO's basemaps.
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    {
      attribution: "&copy; Esri &mdash; Esri, DeLorme, NAVTEQ",
      maxZoom: 16,
      noWrap: true,
    },
  ).addTo(map);

  buildVesselList();
  buildTrackVesselSelect();
  wireControls();
  loadOverview();
}

document.addEventListener("DOMContentLoaded", init);
