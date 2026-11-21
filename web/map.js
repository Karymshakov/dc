"use strict";

const element = (id) => document.getElementById(id);
const data = JSON.parse(element("map-data").textContent);
const basemap = JSON.parse(element("basemap-data").textContent);
const number = new Intl.NumberFormat("ru", { maximumFractionDigits: 2 });
const plural = new Intl.PluralRules("ru");
const cellNoun = { one: "клетка", few: "клетки", many: "клеток", other: "клетки" };
const officialRadius = 1000;
const signed = (value) => number.format(value).replace("-", "−");
const percent = (part, total) => total ? Math.round(100 * part / total) : 0;
const signalColor = { strong: "#258164", fair: "#e2ab2b", weak: "#c74547" };
const signalText = { strong: "Уверенный сигнал", fair: "Средний сигнал", weak: "Слабый сигнал" };
const pairColor = { optimal: "#087f78", greedy: "#8c59a4", custom: "#326a99", shared: "#527997" };
const customColor = {
  "slot-first": "#267a99",
  "slot-second": "#8c59a4",
  "slot-shared": "#809bb8",
  "plan-gain": "#2cae98",
  "plan-kept": "#527997",
  "plan-loss": "#d36971",
  "plan-uncovered": "#805660",
};
const modeText = {
  stress: "Отказ станции",
  signal: "Исходный сигнал",
  optimal: "Полный перебор",
  greedy: "Жадная пара",
  compare: "Сравнение пар",
  custom: "Своя пара",
  buildings: "Застройка",
};
const stressColor = { "stress-retained": "#288b7e", "stress-lost": "#d36971", "stress-uncovered": "#785963" };
const heatStyle = {
  radius: 24,
  blur: 18,
  minOpacity: 0.25,
  gradient: { 0.15: "#376dba", 0.4: "#5da09a", 0.65: "#f0c253", 0.85: "#df7750", 1: "#ae2f41" },
};

const map = L.map("coverage-map", {
  fadeAnimation: false,
  maxZoom: 19,
  zoomSnap: 0.25,
  zoomDelta: 0.75,
});
map.attributionControl.setPrefix(false);
const mapCredit = ['<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a>'];
const source = JSON.stringify(basemap.source ?? basemap.metadata?.source ?? basemap.meta?.source ?? "").toLowerCase();
if (source.includes("openfreemap")) {
  mapCredit.unshift('<a href="https://openfreemap.org/" target="_blank" rel="noopener noreferrer">OpenFreeMap</a>');
}
if (source.includes("openfreemap") || source.includes("openmaptiles")) {
  mapCredit.splice(mapCredit.length - 1, 0, '<a href="https://openmaptiles.org/" target="_blank" rel="noopener noreferrer">© OpenMapTiles</a>');
}
map.attributionControl.addAttribution(mapCredit.join(" · "));
map.fitBounds(data.bounds, { padding: [21, 21] });
L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);

for (const [name, order] of [["basemap", 260], ["online-basemap", 270], ["district", 370], ["radii", 420], ["critical-outline", 434], ["map-label", 440], ["station", 480]]) {
  const pane = map.createPane(name);
  pane.style.zIndex = String(order);
  if (name !== "station") pane.style.pointerEvents = "none";
}
map.createPane("cell-results").style.zIndex = "405";

const roadStyles = {
  major: { color: "#737d79", weight: 2.15, opacity: 0.8 },
  road: { color: "#465555", weight: 1.75, opacity: 0.95 },
  local: { color: "#313d40", weight: 0.9, opacity: 0.88 },
  water: { color: "#46646e", weight: 1.4, fillColor: "#2d4b55", fillOpacity: 1 },
  park: { color: "#32463d", weight: 0.5, fillColor: "#283a33", fillOpacity: 0.93 },
  rail: { color: "#737378", weight: 1.15, dashArray: "3 4", opacity: 0.65 },
};

const baseRenderer = L.canvas({ pane: "basemap", padding: 0.38 });
L.geoJSON(basemap, {
  filter: (feature) => Object.hasOwn(roadStyles, feature.properties.kind),
  renderer: baseRenderer,
  interactive: false,
  style: (feature) => roadStyles[feature.properties.kind],
}).addTo(map);
L.rectangle(data.bounds, {
  pane: "district", color: "#a6b7b6", weight: 1.15, fill: false, dashArray: "4 5", interactive: false,
}).addTo(map);

function roadLabel(coordinates) {
  let length = 0;
  const segments = [];
  for (let index = 1; index < coordinates.length; index += 1) {
    const [west, south] = coordinates[index - 1];
    const [east, north] = coordinates[index];
    const midpoint = [(south + north) / 2, (west + east) / 2];
    const longitudeScale = Math.cos(midpoint[0] * Math.PI / 180);
    const dx = longitudeScale * (east - west);
    const dy = north - south;
    let angle = Math.atan2(-dy, dx) * 180 / Math.PI;
    if (angle < -90) angle += 180;
    if (angle > 90) angle -= 180;
    length += Math.hypot(dx, dy);
    segments.push({ point: midpoint, angle, start: length });
  }
  const middle = segments.find((segment) => segment.start >= length / 2);
  return middle ? { point: middle.point, angle: middle.angle, score: length } : null;
}

const geographicLabels = new Map();
const placeScore = { city: 4, suburb: 3, quarter: 2, water: 1 };
const streetTypes = {
  улица: "улица", "ул.": "улица", проспект: "проспект", "пр-т": "проспект",
  переулок: "переулок", "пер.": "переулок", проезд: "проезд",
  бульвар: "бульвар", "бул.": "бульвар", шоссе: "шоссе",
  көчө: "көчө", көчөсү: "көчө", проспекти: "проспекти",
};

function streetKey(name) {
  const words = name.normalize("NFKC").trim().toLocaleLowerCase("ru").replaceAll("ё", "е").split(/\s+/);
  const first = streetTypes[words[0]];
  const last = streetTypes[words.at(-1)];
  if (first) words.shift();
  else if (last) words.pop();
  return `road:${first ?? last ?? ""}:${words.join(" ")}`;
}

for (const feature of basemap.features || []) {
  const { kind, name, label: placeType } = feature.properties;
  if (!name || !feature.geometry) continue;
  const { type, coordinates } = feature.geometry;
  if (kind === "place" && type === "Point") {
    geographicLabels.set(`place:${name}`, {
      name,
      point: [coordinates[1], coordinates[0]],
      angle: 0,
      score: placeScore[placeType] ?? 2,
      kind: placeType === "water" ? "water" : "place",
      minZoom: placeType === "water" ? 15 : placeType === "quarter" ? 14 : 0,
    });
  }
  if ((kind === "major" || kind === "road") && (type === "LineString" || type === "MultiLineString")) {
    const lines = type === "LineString" ? [coordinates] : coordinates;
    const isMainRoad = kind === "major";
    const key = streetKey(name);
    for (const line of lines) {
      const label = roadLabel(line);
      const score = label ? label.score * (isMainRoad ? 2 : 1) : 0;
      if (score > (geographicLabels.get(key)?.score ?? 0)) {
        geographicLabels.set(key, { ...label, score, name, kind: "road", minZoom: isMainRoad ? 12 : 15 });
      }
    }
  }
}

const labels = L.layerGroup().addTo(map);
const gisToggle = element("toggle-2gis");
const gisDialog = element("gis-key-dialog");
const gisKeyInput = element("gis-key");
const gisStatus = element("gis-status");
let gisLayer;

function showGisStatus(message) {
  gisStatus.textContent = message;
  gisStatus.hidden = !message;
}

function hideGis(message = "") {
  gisLayer.remove();
  labels.addTo(map);
  gisToggle.setAttribute("aria-pressed", "false");
  showGisStatus(message);
}

function showGis() {
  gisToggle.setAttribute("aria-pressed", "true");
  gisLayer.addTo(map);
}

gisToggle.addEventListener("click", () => {
  if (gisLayer && map.hasLayer(gisLayer)) hideGis();
  else if (gisLayer) showGis();
  else gisDialog.showModal();
});

gisDialog.addEventListener("close", () => { gisKeyInput.value = ""; });
gisDialog.querySelector("form").addEventListener("submit", (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  const key = gisKeyInput.value.trim();
  if (!key) return;
  gisDialog.close();

  let loadedTiles = 0;
  const url = `https://tile0.maps.2gis.com/v2/tiles/online_sd/{z}/{x}/{y}.png?key=${encodeURIComponent(key)}`;
  gisLayer = L.tileLayer(url, {
    pane: "online-basemap",
    noWrap: true,
    updateWhenIdle: true,
    referrerPolicy: "no-referrer",
    attribution: '<a href="https://dev.2gis.ru/" target="_blank" rel="noopener noreferrer">© 2ГИС</a>',
  });
  gisLayer.on("loading", () => {
    loadedTiles = 0;
    showGisStatus("Загружаю подложку 2ГИС…");
  });
  gisLayer.on("tileload", () => { loadedTiles += 1; });
  gisLayer.on("load", () => {
    if (loadedTiles) {
      labels.remove();
      showGisStatus("");
    } else {
      hideGis("2ГИС недоступен. Показана офлайн-карта.");
      gisLayer = undefined;
    }
  });
  showGis();
});

const labelClasses = { place: "basemap-place", water: "basemap-water-name", road: "basemap-road-label" };
const measure = document.createElement("canvas").getContext("2d");
const labelMetrics = {};
for (const [kind, className] of Object.entries(labelClasses)) {
  const probe = document.createElement("span");
  probe.className = className;
  map.getContainer().append(probe);
  const style = getComputedStyle(probe);
  labelMetrics[kind] = {
    font: `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`,
    height: Number.parseFloat(style.lineHeight),
  };
  probe.remove();
}
const orderedLabels = [...geographicLabels.values()].sort((first, second) => second.score - first.score);

function labelBounds(label, position) {
  const { height, font } = labelMetrics[label.kind];
  measure.font = font;
  const width = measure.measureText(label.name).width;
  const angle = label.angle * Math.PI / 180;
  const horizontal = Math.abs(Math.cos(angle));
  const vertical = Math.abs(Math.sin(angle));
  const halfWidth = (width * horizontal + height * vertical) / 2 + 5;
  const halfHeight = (width * vertical + height * horizontal) / 2 + 4;
  return {
    left: position.x - halfWidth, right: position.x + halfWidth,
    top: position.y - halfHeight, bottom: position.y + halfHeight,
  };
}

function stationBounds(site, selected) {
  const point = map.latLngToContainerPoint([site.lat, site.lon]);
  const margin = selected ? (activeMode === "custom" ? 28 : 19) : 10;
  return { left: point.x - margin, right: point.x + margin, top: point.y - (selected ? 39 : 10), bottom: point.y + 10 };
}

function drawLabels() {
  labels.clearLayers();
  const size = map.getSize();
  const zoom = map.getZoom();
  const compact = size.x < 535;
  const limit = compact ? (zoom >= 15 ? 6 : 4) : (zoom >= 15 ? 16 : 10);
  const blockers = data.towers.map((tower) => stationBounds(tower, false));
  for (const id of model?.pair ?? []) blockers.push(stationBounds(candidates.get(id), true));
  let visible = 0;
  for (const label of orderedLabels) {
    if (zoom < label.minZoom) continue;
    const position = map.latLngToContainerPoint(label.point);
    if (position.x < 0 || position.y < 0 || position.x > size.x || position.y > size.y) continue;
    const bounds = labelBounds(label, position);
    if (bounds.left < 8 || bounds.top < 10 || bounds.right > size.x - 8 || bounds.bottom > size.y - 28) continue;
    if (bounds.left < 49 && bounds.top < 80) continue;
    if (blockers.some((other) => bounds.left < other.right && bounds.right > other.left &&
      bounds.top < other.bottom && bounds.bottom > other.top)) continue;
    const text = document.createElement("span");
    text.textContent = label.name;
    text.style.position = "absolute";
    text.style.inset = "0 auto auto 0";
    text.style.transform = `translate(-50%, -50%) rotate(${label.angle}deg)`;
    const icon = L.divIcon({ html: text, className: labelClasses[label.kind], iconSize: [0, 0] });
    L.marker(label.point, { pane: "map-label", icon, interactive: false, keyboard: false }).addTo(labels);
    blockers.push(bounds);
    visible += 1;
    if (visible >= limit) break;
  }
}

const modeButtons = [...document.querySelectorAll("button[data-mode]")];
const strategyButtons = [...document.querySelectorAll("button[data-select-mode]")];
const menus = [...document.querySelectorAll(".control-menu")];
const scenarioMenu = element("scenario-menu");
const pairModes = ["compare", "optimal", "greedy"];
const scenarioModes = ["stress", "custom", "buildings"];
const periodControl = element("measurement-period");
const compareFilters = [...document.querySelectorAll("[data-compare-filter]")];
const compareCounts = [...document.querySelectorAll("[data-compare-count]")];
const comparisonRadii = element("show-comparison-radii");
const heatControl = element("show-heat");
const candidateControl = element("show-candidates");
const firstSelect = element("custom-first");
const secondSelect = element("custom-second");
const excludeSelect = element("custom-excluded");
const customCompare = element("compare-custom-baseline");
const customSlotButtons = [...document.querySelectorAll("[data-custom-slot]")];
const customSlotIds = [...document.querySelectorAll("[data-custom-slot-id]")];
const customClearButtons = [...document.querySelectorAll("[data-clear-slot]")];
const customPinButtons = [...document.querySelectorAll("[data-pin-slot]")];
const customCounts = [...document.querySelectorAll("[data-custom-count]")];
const criticalThreshold = element("critical-threshold");
const criticalMapButton = element("critical-map");
const stressFront = element("stress-front");
const stressNormal = element("stress-normal");
const stressFirstFailure = element("stress-fail-first");
const stressSecondFailure = element("stress-fail-second");
const stressWorst = element("stress-worst");
const stressPlanB = element("stress-plan-b");
const stressCounts = [...document.querySelectorAll("[data-stress-count]")];
const radiusControl = element("scenario-radius");
const exportScenarioButton = element("export-scenario");
const radiusDecision = element("radius-decision");
const radiusDecisionRange = element("radius-decision-range");
const radiusDecisionRows = element("radius-decision-rows");
const candidates = new Map(data.candidates.map((candidate) => [candidate.id, candidate]));
const eligibleIds = data.candidates.filter((candidate) => candidate.eligible).map((candidate) => candidate.id).sort();
const customPair = ["", ""];
let activeCustomSlot = 0;
let pinnedCustomSlot = null;
let customNotice = "";
let stressPairKey = "";
let stressScenario = "normal";
let stressManualFailure = "";
let planningRadius = officialRadius;
let criticalOutlineThreshold = null;

const cells = L.layerGroup().addTo(map);
const criticalOutlines = L.layerGroup().addTo(map);
const stationLayer = L.layerGroup().addTo(map);
const radiusLayer = L.layerGroup();
const shapes = new Map();
const stationMarkers = new Map();
const comparisonCategories = ["shared", "optimal", "greedy", "residual"];
const highlightedGroups = new Set(comparisonCategories);
let activeMode = "signal";
let activePeriod = "all";
let heatPoints = [];
let heatLayer = null;
let model;

const buildingInspector = createBuildingInspector({
  map,
  readScene: () => JSON.parse(element("buildings-data").textContent),
  readRadio: () => JSON.parse(element("radio-data").textContent),
  sites: eligibleIds.map((id) => candidates.get(id)),
  layers: data.layers,
  baselinePair: data.result.best_candidates,
  number,
});

function popup(title, details) {
  const content = document.createElement("div");
  content.className = "map-popup";
  const heading = document.createElement("strong");
  heading.textContent = title;
  content.append(heading);
  for (const text of details) {
    const line = document.createElement("div");
    line.textContent = text;
    content.append(line);
  }
  return content;
}

function planningMode() {
  return activeMode === "stress" || activeMode === "custom";
}

function currentRadius() {
  return planningMode() ? planningRadius : officialRadius;
}

function radiusInputs() {
  const radius = currentRadius();
  return radius === officialRadius ? data : data.radius_scenarios[radius];
}

function siteCells(id) {
  const inputs = radiusInputs();
  return inputs === data
    ? candidates.get(id)?.covered_by_period?.[activePeriod] ?? []
    : inputs.coverage_by_period[activePeriod][id] ?? [];
}

function resilienceKey(option) {
  return JSON.stringify(option.ids);
}

function stressCoverage() {
  const options = radiusInputs().resilience_options?.[activePeriod] ?? [];
  const option = options.find((pair) => resilienceKey(pair) === stressPairKey) ?? options[0];
  if (!option) return null;
  stressPairKey = resilienceKey(option);

  const [first, second] = option.ids;
  const worstFailure = option.single_site_cells[second] <= option.single_site_cells[first] ? first : second;
  if (stressScenario === "manual" && !option.ids.includes(stressManualFailure)) {
    stressScenario = "worst";
    stressManualFailure = "";
  }
  const failed = stressScenario === "worst" ? worstFailure : stressScenario === "manual" ? stressManualFailure : "";
  const siteCoverage = option.ids.map(siteCells);
  const nominal = new Set(siteCoverage.flat());
  const retained = failed ? new Set(siteCoverage[failed === first ? 1 : 0]) : nominal;
  return { nominal, retained, failed, worstFailure, option, options };
}

function coverageModel() {
  const optimal = new Set(data.strategies.optimal.covered_by_period[activePeriod]);
  const greedy = new Set(data.strategies.greedy.covered_by_period[activePeriod]);
  const shared = new Set([...optimal].filter((id) => greedy.has(id)));
  const customSlots = customPair.map((id) => new Set(siteCells(id)));
  const [first, second] = customSlots;
  const custom = new Set([...first, ...second]);
  const stress = activeMode === "stress" ? stressCoverage() : null;
  const coverage = { optimal, greedy, custom, stress: stress?.retained };
  const pairByMode = {
    optimal: data.strategies.optimal.ids,
    greedy: data.strategies.greedy.ids,
    custom: customPair.filter(Boolean),
    stress: stress?.option.ids ?? [],
  };
  const compareSites = [...new Set([...pairByMode.optimal, ...pairByMode.greedy])];
  return {
    ...coverage,
    shared,
    customSlots,
    customBaseline: activeMode === "custom" && currentRadius() !== officialRadius
      ? new Set(data.result.best_candidates.flatMap(siteCells))
      : optimal,
    stress,
    covered: coverage[activeMode] ?? new Set(),
    pair: activeMode === "compare" ? compareSites : pairByMode[activeMode] ?? [],
  };
}

function customCellCategory(id) {
  if (customCompare.checked) {
    const inBase = model.customBaseline.has(id);
    const inPair = model.custom.has(id);
    if (inBase && inPair) return "plan-kept";
    if (inPair) return "plan-gain";
    if (inBase) return "plan-loss";
    return "plan-uncovered";
  }
  const [first, second] = model.customSlots;
  if (first.has(id) && second.has(id)) return "slot-shared";
  if (first.has(id)) return "slot-first";
  if (second.has(id)) return "slot-second";
  return "residual";
}

function cellCategory(cell) {
  if (activeMode === "signal") return cell.state;
  if (cell.state !== "weak") return "background";
  if (activeMode === "buildings") return "profile-target";
  if (activeMode === "stress") {
    if (model.stress?.retained.has(cell.id)) return "stress-retained";
    if (model.stress?.nominal.has(cell.id)) return "stress-lost";
    return "stress-uncovered";
  }
  if (activeMode === "custom") return customCellCategory(cell.id);
  if (activeMode !== "compare") return model.covered.has(cell.id) ? "covered" : "residual";
  if (model.shared.has(cell.id)) return "shared";
  if (model.optimal.has(cell.id)) return "optimal";
  if (model.greedy.has(cell.id)) return "greedy";
  return "residual";
}

function cellStyle(category) {
  if (category === "background") {
    return { color: "#7e9193", weight: 0.55, opacity: 0.24, fillColor: "#91aaad", fillOpacity: 0.04 };
  }
  if (category === "profile-target") {
    return { color: "#b3a296", weight: .8, opacity: .55, fillColor: "#b3a296", fillOpacity: .07 };
  }
  const color = signalColor[category] ?? pairColor[category] ?? customColor[category] ?? stressColor[category] ??
    (category === "covered" ? pairColor[activeMode] : "#ad3544");
  const dashArray = {
    greedy: "2 3", residual: "4 3", "plan-loss": "4 3", "plan-uncovered": "2 4",
    "stress-lost": "4 3", "stress-uncovered": "2 4",
  }[category] ?? null;
  if (activeMode === "compare" && !highlightedGroups.has(category)) {
    return { color, fillColor: color, weight: 0.6, opacity: 0.3, fillOpacity: 0.08, dashArray };
  }
  const opacityByGroup = {
    shared: 0.51, optimal: 0.72, greedy: 0.72, residual: 0.53, "plan-uncovered": 0.23, "plan-kept": 0.49,
    "stress-retained": 0.72, "stress-lost": 0.66, "stress-uncovered": 0.24,
  };
  const fillOpacity = heatControl.checked ? 0.23 : opacityByGroup[category] ?? 0.62;
  if (activeMode === "signal") return { color, fillColor: color, weight: 0.5, opacity: 0.42, fillOpacity };
  return { color, fillColor: color, weight: category === "residual" ? 1.15 : 0.85, opacity: 0.92, fillOpacity, dashArray };
}

function isCriticalOutline(cell) {
  return activeMode === "custom" && criticalOutlineThreshold !== null &&
    cell.state === "weak" && cell.median <= criticalOutlineThreshold;
}

function cellDetails(cell) {
  const details = [
    `Замеры · медиана ${signed(cell.median)} дБм · ${signalText[cell.state].toLowerCase()}`,
    `Замеров в клетке — ${number.format(cell.count)}`,
  ];
  if (cell.state !== "weak") return details;
  if (isCriticalOutline(cell)) details.push(`Критическая клетка · медиана ≤ ${signed(criticalOutlineThreshold)} дБм`);
  if (activeMode === "stress") {
    const stressLabel = {
      "stress-retained": model.stress?.failed ? `Остаётся в охвате при отказе ${model.stress.failed}` : "В штатном охвате пары",
      "stress-lost": `Выпадает из охвата при отказе ${model.stress?.failed}`,
      "stress-uncovered": "Вне штатного охвата выбранной пары",
    };
    details.push(stressLabel[cellCategory(cell)]);
  } else if (activeMode === "custom") {
    const customLabel = {
      "slot-first": "Покрывает только первая площадка",
      "slot-second": "Покрывает только вторая площадка",
      "slot-shared": "Покрывают обе площадки",
      residual: "Вне радиусов выбранных площадок",
      "plan-gain": "Новая покрытая клетка относительно базы",
      "plan-kept": "Остаётся покрытой",
      "plan-loss": "В базовом охвате, вне новой пары",
      "plan-uncovered": "Вне радиусов обеих пар",
    };
    details.push(customLabel[customCellCategory(cell.id)]);
  } else if (activeMode === "compare") {
    if (model.shared.has(cell.id)) details.push("В радиусах обеих пар");
    else if (model.optimal.has(cell.id)) details.push("В радиусах только пары перебора");
    else if (model.greedy.has(cell.id)) details.push("В радиусах только жадной пары");
    else details.push("Вне радиусов обеих пар");
  } else if (activeMode === "optimal" || activeMode === "greedy") {
    details.push(model.covered.has(cell.id) ? "Центр клетки в пределах 1 км от площадки пары" : "Центр клетки дальше 1 км от обеих площадок пары");
  }
  return details;
}

function cellPopup(cell) {
  const card = popup(cell.id, cellDetails(cell));
  if (cell.state === "weak") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "compare-shortcut";
    button.textContent = "Проверить здания на пути";
    button.addEventListener("click", () => {
      const preferredSites = model.pair;
      activeMode = "buildings";
      renderView({ requestedCellId: cell.id, preferredSites });
      element("buildings-panel").scrollIntoView({ block: "nearest" });
      element("buildings-heading").focus({ preventScroll: true });
    });
    card.append(button);
  }
  return card;
}

function renderHeatLayer() {
  heatLayer?.remove();
  const visible = activeMode !== "buildings" && heatControl.checked;
  heatLayer = visible ? L.heatLayer(heatPoints, heatStyle).addTo(map) : null;
  element("heat-legend").hidden = !visible;
}

function renderCells() {
  cells.clearLayers();
  criticalOutlines.clearLayers();
  shapes.clear();
  heatPoints = [];
  for (const cell of data.layers[activePeriod]) {
    if (activeMode === "buildings" && cell.state !== "weak") continue;
    const rectangle = L.rectangle(cell.bounds, {
      ...cellStyle(cellCategory(cell)),
      pane: "cell-results",
    }).addTo(cells);
    if (activeMode === "buildings") {
      rectangle.on("click", () => buildingInspector.selectCell(cell.id));
    } else {
      rectangle.bindPopup(() => cellPopup(cell));
    }
    rectangle.on("mouseover", () => rectangle.setStyle({ weight: 2.15, color: "#d5edef", opacity: 1 }));
    rectangle.on("mouseout", () => rectangle.setStyle(cellStyle(cellCategory(cell))));
    shapes.set(cell.id, rectangle);

    if (isCriticalOutline(cell)) {
      L.rectangle(cell.bounds, {
        pane: "critical-outline", className: "critical-cell-outline",
        color: "#edcb81", weight: 2.2, fill: false, opacity: 0.96, dashArray: "4 2", interactive: false,
      }).addTo(criticalOutlines);
    }
    if (cell.state === "weak") {
      const center = rectangle.getBounds().getCenter();
      heatPoints.push([center.lat, center.lng, 1]);
    }
  }
  const criticalLegend = element("critical-legend");
  criticalLegend.hidden = criticalOutlines.getLayers().length === 0;
  if (!criticalLegend.hidden) {
    element("critical-legend-threshold").textContent = signed(criticalOutlineThreshold);
  }
  renderHeatLayer();
}

function renderCompareFilters() {
  for (const button of compareFilters) {
    button.setAttribute("aria-pressed", String(highlightedGroups.has(button.dataset.compareFilter)));
  }
}

function updateCellStyles() {
  for (const cell of data.layers[activePeriod]) {
    shapes.get(cell.id).setStyle(cellStyle(cellCategory(cell)));
  }
}

function updateCompareFocus() {
  renderCompareFilters();
  updateCellStyles();
}

function focusLayer(layer, location, zoom) {
  map.setView(location, Math.max(map.getZoom(), zoom), { animate: false });
  layer.openPopup(location);
  map.getContainer().scrollIntoView({ block: "nearest" });
}

for (const tower of data.towers) {
  L.circleMarker([tower.lat, tower.lon], {
    pane: "station", radius: 6, color: "#fff", weight: 2, fillColor: "#253b43", fillOpacity: 1,
  }).addTo(map).bindPopup(() => popup(tower.id, ["Действующая базовая станция"]));
}

function markerGroup(candidate) {
  if (activeMode === "stress") return candidate.id === model.stress?.failed ? "stress-lost" : "stress-retained";
  if (activeMode === "custom") return candidate.id === customPair[0] ? "slot-first" : "slot-second";
  if (activeMode !== "compare") return activeMode;
  const inOptimal = data.strategies.optimal.ids.includes(candidate.id);
  const inGreedy = data.strategies.greedy.ids.includes(candidate.id);
  return inOptimal && inGreedy ? "shared" : inOptimal ? "optimal" : "greedy";
}

function renderRadii() {
  const visible = model.pair.length > 0 && (activeMode !== "compare" || comparisonRadii.checked);
  if (visible) radiusLayer.addTo(map);
  else radiusLayer.remove();
  element("ring-legend").hidden = !visible;
  element("ring-distance").textContent = `${number.format(currentRadius())} м`;
  element("comparison-radii-control").hidden = activeMode !== "compare";
}

function renderStations() {
  stationLayer.clearLayers();
  radiusLayer.clearLayers();
  stationMarkers.clear();
  for (const candidate of data.candidates) {
    const selected = model.pair.includes(candidate.id);
    const custom = activeMode === "custom";
    if (!selected && !candidateControl.checked && !(custom && candidate.eligible)) continue;
    const excluded = custom && candidate.id === excludeSelect.value;
    const failed = activeMode === "stress" && candidate.id === model.stress?.failed;
    const unavailable = excluded || failed;
    const available = candidate.eligible && !unavailable;
    const point = [candidate.lat, candidate.lon];
    const group = selected ? markerGroup(candidate) : null;
    const color = pairColor[group] ?? customColor[group] ?? stressColor[group] ??
      (unavailable ? "#d36971" : available ? "#85bdc2" : "#81908f");
    const slotNumber = selected && custom ? customPair.indexOf(candidate.id) + 1 : null;
    const title = slotNumber ? `${slotNumber} · ${candidate.id}` : candidate.id;
    const marker = L.circleMarker(point, {
      pane: "station",
      radius: selected ? 8 : custom ? 6 : 4.5,
      color: selected && !failed ? "#e6f5f4" : color,
      weight: selected ? 2.5 : 1.6,
      fillColor: selected || available ? color : "#172731",
      fillOpacity: selected && !failed ? 1 : available ? 0.55 : 0.4,
      opacity: available ? 1 : 0.75,
    }).addTo(stationLayer);
    const markerElement = marker.getElement();
    markerElement.dataset.siteId = candidate.id;
    if (selected && activeMode === "stress") markerElement.dataset.stressSite = failed ? "failed" : "active";
    const details = [
      selected ? (custom ? `Площадка ${slotNumber} в вашей паре`
        : activeMode === "stress" ? (failed ? "Отказ в сценарии · без замены" : "Площадка работает")
          : `В расчёте · ${group === "shared" ? "обе пары" : modeText[group].toLowerCase()}`)
        : excluded ? "Недоступна для этого плана"
          : available ? "Допустимая площадка" : "Ближе 500 м к действующей БС",
      candidate.nearest_m == null
        ? "Действующих БС нет"
        : `До ближайшей БС ${number.format(Math.round(candidate.nearest_m))} м`,
    ];
    if (available) {
      details.push(`Слабых клеток в радиусе ${number.format(currentRadius())} м: ${number.format(siteCells(candidate.id).length)}`);
    }
    if (custom || activeMode === "stress") {
      details.push(`${candidate.lat.toFixed(5)}, ${candidate.lon.toFixed(5)}`);
    }
    if (custom && available && !selected) {
      marker.on("click", () => {
        if (activeCustomSlot !== null) {
          setCustomSite(activeCustomSlot, candidate.id);
        } else {
          L.popup().setLatLng(point).setContent(popup(title, details)).openOn(map);
        }
      });
    } else {
      marker.bindPopup(() => popup(title, details));
    }
    stationMarkers.set(candidate.id, marker);

    if (selected && !failed) {
      L.circle(point, {
        pane: "radii",
        radius: currentRadius(),
        color,
        weight: 2,
        opacity: 0.92,
        fill: false,
        dashArray: group === "greedy" ? "3 6" : "9 5",
        interactive: false,
      }).addTo(radiusLayer);
    }
    if (selected || excluded) {
      const siteLabel = document.createElement("span");
      siteLabel.textContent = unavailable ? `× ${candidate.id}` : title;
      marker.bindTooltip(siteLabel, {
        pane: "station", direction: unavailable ? "right" : "top",
        offset: unavailable ? [5, 0] : [0, -3], permanent: true,
        className: unavailable ? "station-label is-excluded" : "station-label",
      });
    }
  }

  const sites = element("site-pair");
  sites.replaceChildren();
  element("chosen-sites").hidden = !model.pair.length;
  element("sites-heading").textContent = activeMode === "compare" ? "Площадки обеих пар" : "Площадки пары";
  for (const id of model.pair) {
    const marker = stationMarkers.get(id);
    if (!marker) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `point-link is-${markerGroup(candidates.get(id))}`;
    button.textContent = activeMode === "custom" ? `${customPair.indexOf(id) + 1} · ${id}`
      : id === model.stress?.failed ? `× ${id}` : id;
    button.setAttribute("aria-label", `Показать площадку ${id}`);
    button.addEventListener("click", () => focusLayer(marker, marker.getLatLng(), 15));
    sites.append(button);
  }
  renderRadii();
}

function updateCustomSelectors() {
  for (const [index, select] of [firstSelect, secondSelect].entries()) {
    select.replaceChildren();
    select.append(new Option("Выбрать из списка", "", false, !customPair[index]));
    for (const id of eligibleIds) {
      if (id === customPair[1 - index] || id === excludeSelect.value) continue;
      select.append(new Option(id, id, false, id === customPair[index]));
    }
  }
}

function renderCustomControls() {
  updateCustomSelectors();
  for (const button of customSlotButtons) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.customSlot) === activeCustomSlot));
  }
  for (const label of customSlotIds) {
    label.textContent = customPair[Number(label.dataset.customSlotId)] || "Выбрать";
  }
  for (const button of customClearButtons) {
    button.disabled = !customPair[Number(button.dataset.clearSlot)];
  }
  for (const button of customPinButtons) {
    const slot = Number(button.dataset.pinSlot);
    const locked = slot === pinnedCustomSlot;
    button.disabled = !customPair[slot];
    button.setAttribute("aria-pressed", String(locked));
    button.textContent = locked ? "Закреплена" : "Закрепить";
    button.title = locked ? `Снять закрепление площадки ${slot + 1}` : `Сохранить площадку ${slot + 1} при подборе`;
    button.setAttribute("aria-label", button.title);
  }
  customCompare.disabled = customPair.some((id) => !id);
  if (customCompare.disabled) customCompare.checked = false;
  const instruction = activeCustomSlot === null
    ? "Выберите площадку 1 или 2, чтобы заменить её."
    : `Для площадки ${activeCustomSlot + 1} выберите точку на карте или из списка.`;
  element("custom-status").textContent = customNotice || instruction;
}

function setCustomSite(slot, id) {
  if (id && (!candidates.get(id)?.eligible || id === excludeSelect.value || id === customPair[1 - slot])) return;
  if (id !== customPair[slot] && pinnedCustomSlot === slot) pinnedCustomSlot = null;
  customPair[slot] = id;
  customNotice = "";
  if (!id) activeCustomSlot = slot;
  else activeCustomSlot = customPair[1 - slot] ? null : 1 - slot;
  renderView();
}

function suggestCustomPair() {
  const lockedId = pinnedCustomSlot === null ? "" : customPair[pinnedCustomSlot];
  const suggestion = radiusInputs().pair_rankings[activePeriod].find(({ ids }) =>
    !ids.includes(excludeSelect.value) && (!lockedId || ids.includes(lockedId)),
  );
  if (!suggestion) {
    customNotice = "Для выбранных условий нет допустимой пары.";
    return false;
  }
  if (lockedId) {
    customPair[1 - pinnedCustomSlot] = suggestion.ids.find((id) => id !== lockedId);
  } else {
    [customPair[0], customPair[1]] = suggestion.ids;
  }
  activeCustomSlot = null;
  customNotice = "Пара подобрана. Для замены выберите площадку 1 или 2.";
  return true;
}

function setExclusionOptions(unavailable = "") {
  excludeSelect.replaceChildren(new Option("Нет", ""), ...eligibleIds.map((id) => new Option(id, id)));
  excludeSelect.value = unavailable;
}

function openReplacementPlan(anchor, unavailable) {
  criticalOutlineThreshold = null;
  planningRadius = currentRadius();
  activeMode = "custom";
  setExclusionOptions(unavailable);
  customPair[0] = anchor;
  customPair[1] = "";
  pinnedCustomSlot = 0;
  activeCustomSlot = 1;
  customCompare.checked = suggestCustomPair();
  renderView();
  element("custom-pair").scrollIntoView({ block: "nearest" });
}

function openPairComparison(ids, radius, notice) {
  planningRadius = radius;
  activeMode = "custom";
  setExclusionOptions();
  [customPair[0], customPair[1]] = ids;
  pinnedCustomSlot = null;
  activeCustomSlot = null;
  customCompare.checked = true;
  customNotice = notice;
  renderView();
  const mapContainer = map.getContainer();
  mapContainer.scrollIntoView({ block: "nearest" });
  mapContainer.focus({ preventScroll: true });
}

function selectedCriticalDecision() {
  return data.critical_decisions?.[activePeriod]?.[criticalThreshold.value];
}

function renderCriticalDecision() {
  const decision = selectedCriticalDecision();
  const panel = element("critical-panel");
  panel.hidden = activeMode !== "compare" || !decision;
  if (panel.hidden) return;

  const noCriticalCells = decision.critical_total === 0;
  element("critical-empty").hidden = !noCriticalCells;
  element("critical-comparison").hidden = noCriticalCells;
  if (noCriticalCells) return;

  const criticalTotal = number.format(decision.critical_total);
  const baselineIds = data.result.best_candidates;
  element("critical-chosen-ids").textContent = decision.ids.join(" + ");
  element("critical-base-ids").textContent = baselineIds.join(" + ");
  element("critical-chosen-critical").textContent = `${number.format(decision.chosen_critical)} из ${criticalTotal}`;
  element("critical-base-critical").textContent = `${number.format(decision.baseline_critical)} из ${criticalTotal}`;
  element("critical-chosen-weak").textContent = number.format(decision.chosen_weak);
  element("critical-base-weak").textContent = number.format(decision.baseline_weak);
  const criticalChange = decision.chosen_critical - decision.baseline_critical;
  const weakChange = decision.chosen_weak - decision.baseline_weak;
  const delta = (value) => value > 0 ? `+${number.format(value)}` : signed(value);
  let difference = `Критические ${delta(criticalChange)} · все слабые ${delta(weakChange)}`;
  if (decision.ids.join() === baselineIds.join()) difference = "Совпадает с базовой парой";
  else if (!criticalChange && !weakChange) difference = "Число покрытых клеток совпадает";
  element("critical-difference").textContent = difference;
}

function openCriticalDecision() {
  const decision = selectedCriticalDecision();
  if (activeMode !== "compare" || !decision?.critical_total) return;
  const center = map.getCenter();
  criticalOutlineThreshold = Number(criticalThreshold.value);
  openPairComparison(decision.ids, officialRadius,
    `Обведены критические клетки с медианой ≤ ${signed(criticalOutlineThreshold)} дБм.`);
  const gained = data.layers[activePeriod].filter((cell) =>
    isCriticalOutline(cell) && model.custom.has(cell.id) && !model.customBaseline.has(cell.id),
  );
  if (!gained.length) {
    map.panTo(center, { animate: false });
    return;
  }
  const bounds = L.latLngBounds(gained.flatMap((cell) => cell.bounds));
  map.fitBounds(bounds, { padding: [32, 36], maxZoom: 15.5, animate: false });
}

function coverageChanges(reference, covered) {
  return {
    retained: [...covered].filter((id) => reference.has(id)),
    gained: [...covered].filter((id) => !reference.has(id)),
    lost: [...reference].filter((id) => !covered.has(id)),
  };
}

function renderCustomSummary(totalWeak) {
  const [first, second] = model.customSlots;
  const shared = first.size + second.size - model.custom.size;
  const changes = coverageChanges(model.customBaseline, model.custom);
  const gain = changes.gained.length;
  const kept = changes.retained.length;
  const loss = changes.lost.length;
  const counts = {
    first: first.size - shared,
    shared,
    second: second.size - shared,
    residual: totalWeak - model.custom.size,
    gain,
    kept,
    loss,
    uncovered: totalWeak - model.custom.size - loss,
  };
  for (const counter of customCounts) {
    counter.textContent = number.format(counts[counter.dataset.customCount]);
  }
  const comparing = activeMode === "custom" && customCompare.checked;
  element("custom-legend").hidden = activeMode !== "custom" || comparing;
  element("custom-baseline-legend").hidden = !comparing;
  const impact = element("custom-plan-impact");
  impact.hidden = !comparing;
  const changed = gain + loss;
  const changedNoun = plural.select(changed) === "one" ? "клетки" : "клеток";
  impact.textContent = `Охват изменился у ${number.format(changed)} ${changedNoun} · +${number.format(gain)} / −${number.format(loss)}`;
}

function renderRadiusControls() {
  const visible = planningMode();
  element("radius-controls").hidden = !visible;
  exportScenarioButton.hidden = !visible;
  exportScenarioButton.disabled = !visible || model.pair.length !== 2;
  renderRadiusDecision();
  if (!visible) return;

  const radius = currentRadius();
  radiusControl.value = radius;
  element("scenario-radius-value").textContent = `${number.format(radius)} м`;
  const sensitivity = data.radius_sensitivity?.[activePeriod]?.find((row) => row.radius_m === radius);
  element("radius-best").textContent = sensitivity
    ? `${sensitivity.best_ids.join(" + ")} · ${number.format(sensitivity.best_covered)}`
    : "—";
  element("radius-baseline").textContent = sensitivity
    ? `${data.result.best_candidates.join(" + ")} · ${number.format(sensitivity.baseline_covered)}`
    : "—";
}

function selectedRadiusDecision() {
  return data.radius_decisions?.[activePeriod]?.[radiusDecisionRange.value];
}

function gapLabel(gap) {
  if (!gap) return "Равна лидеру при всех радиусах";
  const cells = plural.select(gap) === "one" ? "клетки" : "клеток";
  return `До ${number.format(gap)} ${cells} меньше лидера`;
}

function renderRadiusDecision() {
  const decision = selectedRadiusDecision();
  radiusDecision.hidden = !planningMode() || !decision;
  if (radiusDecision.hidden || !radiusDecision.open) return;

  element("radius-decision-chosen").textContent = `Устойчивая · ${decision.ids.join(" + ")}`;
  element("radius-decision-gap").textContent = gapLabel(decision.max_gap);
  element("radius-decision-base").textContent = `Базовая · ${data.result.best_candidates.join(" + ")}`;
  element("radius-decision-base-gap").textContent = gapLabel(decision.baseline_max_gap);

  const rows = decision.rows.map((row) => {
    const tr = document.createElement("tr");
    tr.classList.toggle("is-current", row.radius_m === currentRadius());
    const header = document.createElement("th");
    header.scope = "row";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${number.format(row.radius_m)} м`;
    button.setAttribute("aria-label", `Сравнить пары при радиусе ${number.format(row.radius_m)} м`);
    button.addEventListener("click", () => openRadiusDecision(row.radius_m));
    header.append(button);
    tr.append(header);
    for (const value of [row.leader_covered, row.chosen_covered, row.baseline_covered]) {
      const cell = document.createElement("td");
      cell.textContent = number.format(value);
      tr.append(cell);
    }
    return tr;
  });
  radiusDecisionRows.replaceChildren(...rows);
}

function openRadiusDecision(radius) {
  const decision = selectedRadiusDecision();
  if (!planningMode() || !decision) return;
  const worstBaseline = decision.baseline_max_gap && decision.rows.find((row) =>
    row.leader_covered - row.baseline_covered === decision.baseline_max_gap,
  );
  const chosenRadius = radius ?? (worstBaseline ? worstBaseline.radius_m : officialRadius);
  const [lower, upper] = radiusDecisionRange.value.split("-").map(Number);
  criticalOutlineThreshold = null;
  openPairComparison(decision.ids, chosenRadius,
    `Устойчивая пара для диапазона ${number.format(lower)}–${number.format(upper)} м.`);
}

function siteLocation(id) {
  const { lat, lon } = candidates.get(id);
  return { id, lat, lon };
}

function exportScenario() {
  if (!planningMode() || model.pair.length !== 2) return;
  const stress = model.stress;
  const failed = stress?.failed || null;
  const fixed = pinnedCustomSlot === null || stress ? null : customPair[pinnedCustomSlot];
  const unavailable = !stress && excludeSelect.value ? siteLocation(excludeSelect.value) : null;
  const reference = stress ? stress.nominal : model.customBaseline;
  const referenceIds = stress ? stress.option.ids : data.result.best_candidates;
  const covered = model.covered;
  const changes = coverageChanges(reference, covered);
  const radius = currentRadius();
  const report = {
    schema_version: 1,
    data_source: "synthetic",
    mode: activeMode,
    period: activePeriod,
    radius_m: radius,
    geometry: {
      distance_method: "haversine",
      coverage_target: "weak_cell_center",
      coverage_distance_inclusive: true,
      weak_median_rsrp_below_dbm: -105,
      min_measurements_per_cell: 5,
      min_distance_to_existing_site_m: 500,
    },
    sites: model.pair.map((id) => ({
      ...siteLocation(id),
      status: stress ? (id === failed ? "failed" : "working") : "planned",
      fixed: id === fixed,
    })),
    scenario: {
      status: stress ? stressScenario : "planned",
      failed_site_id: failed,
      fixed_site_id: fixed,
      excluded_site: unavailable,
    },
    reference: {
      type: stress ? "same_pair_working" : "official_pair_at_scenario_radius",
      sites: referenceIds.map(siteLocation),
      covered_weak_cells: reference.size,
    },
    coverage: {
      total_weak_cells: data.summaries[activePeriod].weak_cells,
      covered_weak_cells: covered.size,
      retained_weak_cells: changes.retained.length,
      gained_weak_cells: changes.gained.length,
      lost_weak_cells: changes.lost.length,
      retained_cell_ids: changes.retained.sort(),
      gained_cell_ids: changes.gained.sort(),
      lost_cell_ids: changes.lost.sort(),
    },
  };
  const content = new Blob([JSON.stringify(report, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(content);
  const link = document.createElement("a");
  link.href = url;
  link.download = `coverage-${activeMode}-${activePeriod}-${radius}m.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function renderStressSummary(totalWeak) {
  const visible = activeMode === "stress";
  const stress = model.stress;
  element("stress-panel").hidden = !visible;
  element("stress-legend").hidden = !visible || !stress;
  stressPlanB.hidden = !visible || !stress?.failed;
  if (!visible) return;

  const restoreFocus = stressFront.contains(document.activeElement);
  stressFront.replaceChildren();
  element("stress-actions").hidden = !stress;
  const delta = element("stress-delta");
  delta.hidden = !stress || stress.option === stress.options[0];
  if (!stress) {
    stressFront.textContent = "Нет допустимых пар для этого периода.";
    return;
  }

  const maxNominal = stress.options[0].covered_weak_cells;
  let selectedButton;
  for (const option of stress.options) {
    const button = document.createElement("button");
    const key = resilienceKey(option);
    const selected = option === stress.option;
    button.type = "button";
    button.className = "stress-row";
    button.dataset.stressPair = key;
    button.setAttribute("aria-pressed", String(selected));
    for (const [name, text] of [
      ["pair", option.ids.join(" + ")],
      ["normal", number.format(option.covered_weak_cells)],
      ["worst", number.format(option.worst_single_site_cells)],
    ]) {
      const field = document.createElement("span");
      field.className = `stress-${name}`;
      field.textContent = text;
      button.append(field);
    }
    button.style.setProperty("--stress-normal-share", `${percent(option.covered_weak_cells, maxNominal)}%`);
    button.style.setProperty("--stress-worst-share", `${percent(option.worst_single_site_cells, maxNominal)}%`);
    button.addEventListener("click", () => {
      if (key === stressPairKey) return;
      stressPairKey = key;
      renderView();
    });
    stressFront.append(button);
    if (selected) selectedButton = button;
  }
  if (restoreFocus) selectedButton?.focus({ preventScroll: true });

  const [first, second] = stress.option.ids;
  stressNormal.setAttribute("aria-pressed", String(stressScenario === "normal"));
  stressFirstFailure.textContent = `Отказ ${first}`;
  stressFirstFailure.setAttribute("aria-pressed", String(stressScenario === "manual" && stress.failed === first));
  stressSecondFailure.textContent = `Отказ ${second}`;
  stressSecondFailure.setAttribute("aria-pressed", String(stressScenario === "manual" && stress.failed === second));
  stressWorst.setAttribute("aria-pressed", String(stressScenario === "worst"));
  stressWorst.title = `Отключить ${stress.worstFailure} для этой пары`;

  const direction = (value) => value > 0 ? `+${number.format(value)}` : signed(value);
  const worstGain = stress.option.worst_single_site_cells - stress.options[0].worst_single_site_cells;
  delta.textContent = `${direction(stress.option.covered_weak_cells - maxNominal)} в норме · ${direction(worstGain)} при худшем отказе`;
  const counts = {
    retained: stress.retained.size,
    lost: stress.nominal.size - stress.retained.size,
    uncovered: totalWeak - stress.nominal.size,
  };
  for (const counter of stressCounts) {
    counter.textContent = number.format(counts[counter.dataset.stressCount]);
  }
  element("stress-retained-label").textContent = stress.failed ? "Сохранилось" : "В охвате";
}

function renderSummary() {
  const summary = data.summaries[activePeriod];
  const totalWeak = summary.weak_cells;
  const remaining = totalWeak - model.covered.size;
  const optimalOnly = model.optimal.size - model.shared.size;
  const greedyOnly = model.greedy.size - model.shared.size;
  const residual = totalWeak - model.optimal.size - model.greedy.size + model.shared.size;
  const choosing = ["optimal", "greedy", "custom"].includes(activeMode);

  element("mode-label").textContent = activeMode === "signal" ? "Пара по ТЗ" : modeText[activeMode];
  element("measurement-count").textContent = number.format(summary.measurements);
  element("cell-count").textContent = number.format(summary.cells);

  let metric = number.format(totalWeak);
  let label = "слабых клеток";
  let caption = "Медиана RSRP ниже −105 дБм";
  if (choosing) {
    metric = number.format(model.covered.size);
    label = `из ${number.format(totalWeak)} слабых клеток в радиусах пары`;
    caption = `Вне радиусов ${number.format(remaining)} · охват ${percent(model.covered.size, totalWeak)}%`;
  }
  if (activeMode === "signal") {
    metric = number.format(model.optimal.size);
    label = `из ${number.format(totalWeak)} слабых клеток в пределах 1 км от площадок пары`;
    caption = data.result.best_candidates.join(" + ");
    if (activePeriod !== "all") caption += " · охват за выбранный период";
  }
  if (activeMode === "custom") {
    if (!model.pair.length) {
      metric = "—";
      label = "Нет выбранных площадок";
      caption = "Выберите площадки 1 и 2";
    } else if (model.pair.length === 1) {
      label = `из ${number.format(totalWeak)} слабых клеток в радиусе одной площадки`;
    } else if (customCompare.checked) {
      const delta = model.custom.size - model.customBaseline.size;
      const deltaText = delta > 0 ? `+${number.format(delta)}` : signed(delta);
      caption = `Базовый охват при ${number.format(currentRadius())} м — ${number.format(model.customBaseline.size)} · изменение ${deltaText}`;
    }
  }
  if (activeMode === "stress") {
    const stress = model.stress;
    if (!stress) {
      metric = "—";
      label = "Нет пары для проверки";
      caption = "";
    } else {
      const retained = stress.retained.size;
      metric = number.format(retained);
      if (stress.failed) {
        label = `из ${number.format(stress.nominal.size)} клеток остаются в охвате`;
        caption = `Отказ ${stress.failed} · потеряно ${number.format(stress.nominal.size - retained)}`;
      } else {
        label = `${cellNoun[plural.select(retained)]} в штатном охвате`;
        caption = `Худший отказ ${stress.worstFailure} · остаётся ${number.format(stress.option.worst_single_site_cells)}`;
      }
    }
  }
  element("metric-value").textContent = metric;
  element("metric-label").textContent = label;
  element("metric-caption").textContent = caption;
  element("coverage-meter").hidden = !choosing || !model.pair.length;
  element("coverage-fill").style.width = `${percent(model.covered.size, totalWeak)}%`;
  element("coverage-fill").style.background = pairColor[activeMode] ?? "";

  for (const strategy of ["optimal", "greedy"]) {
    element(`${strategy}-count`).textContent = number.format(model[strategy].size);
    element(`${strategy}-pair`).textContent = data.strategies[strategy].ids.join(" + ");
  }
  const categoryCounts = { shared: model.shared.size, optimal: optimalOnly, greedy: greedyOnly, residual };
  for (const counter of compareCounts) {
    counter.textContent = number.format(categoryCounts[counter.dataset.compareCount]);
  }
  element("strategy-heading").parentElement.hidden = !pairModes.includes(activeMode);
  element("comparison-source").hidden = activePeriod === "all";
  element("signal-legend").hidden = activeMode !== "signal";
  element("strategy-legend").hidden = activeMode !== "optimal" && activeMode !== "greedy";
  element("covered-chip").style.backgroundColor = pairColor[activeMode] ?? "";
  element("compare-legend").hidden = activeMode !== "compare";
  element("custom-pair").hidden = activeMode !== "custom";
  renderCustomSummary(totalWeak);
  renderStressSummary(totalWeak);
  renderRadiusControls();
  renderCriticalDecision();
}

function renderRanking() {
  const ranking = element("ranked-cells");
  const worstCells = activePeriod === "all"
    ? data.result.worst_cells
    : [...data.layers[activePeriod]]
      .sort((a, b) => a.median - b.median || (a.id < b.id ? -1 : 1))
      .slice(0, 3)
      .map((cell) => ({ cell_id: cell.id, rsrp_median: cell.median }));

  ranking.replaceChildren();
  for (const { cell_id: id, rsrp_median: median } of worstCells) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    const label = document.createElement("span");
    label.textContent = id;
    const value = document.createElement("strong");
    value.textContent = `${signed(median)} дБм`;
    button.append(label, value);
    button.addEventListener("click", () => {
      if (activeMode === "buildings") {
        buildingInspector.selectCell(id, true);
        return;
      }
      const rectangle = shapes.get(id);
      if (rectangle) focusLayer(rectangle, rectangle.getBounds().getCenter(), 16);
    });
    item.append(button);
    ranking.append(item);
  }
  if (!worstCells.length) {
    const item = document.createElement("li");
    item.textContent = "Нет клеток с пятью замерами";
    ranking.append(item);
  }
}

function renderView({ requestedCellId, preferredSites } = {}) {
  map.closePopup();
  renderCustomControls();
  model = coverageModel();
  document.documentElement.dataset.mode = activeMode;
  for (const button of modeButtons) {
    const pressed = button.dataset.mode === "compare" ? pairModes.includes(activeMode) : button.dataset.mode === activeMode;
    button.setAttribute("aria-pressed", String(pressed));
  }
  for (const button of strategyButtons) {
    if (button.classList.contains("strategy-row")) button.setAttribute("aria-pressed", String(button.dataset.selectMode === activeMode));
  }
  scenarioMenu.toggleAttribute("data-active", scenarioModes.includes(activeMode));
  periodControl.value = activePeriod;
  const periodLabel = { day: " · День", night: " · Ночь" }[activePeriod] ?? "";
  element("dataset-note").textContent = `Учебные замеры${periodLabel}`;
  renderCells();
  renderStations();
  renderSummary();
  renderCompareFilters();
  renderRanking();
  map.invalidateSize({ pan: false });
  buildingInspector.render(activeMode === "buildings", activePeriod, requestedCellId, preferredSites);
  drawLabels();
}

for (const button of [...modeButtons, ...strategyButtons]) {
  button.addEventListener("click", () => {
    const mode = button.dataset.mode ?? button.dataset.selectMode;
    const menu = button.closest("details");
    if (menu) {
      menu.open = false;
      menu.querySelector("summary").focus({ preventScroll: true });
    }
    if (mode !== activeMode) {
      activeMode = mode;
      renderView();
    }
    if (button.dataset.selectMode) map.getContainer().scrollIntoView({ block: "nearest" });
  });
}
document.addEventListener("click", (event) => {
  for (const menu of menus) {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const menu = menus.find((item) => item.open && item.contains(event.target));
  if (menu) {
    menu.open = false;
    menu.querySelector("summary").focus();
  }
});
periodControl.addEventListener("change", () => {
  activePeriod = periodControl.value;
  customNotice = "";
  scenarioMenu.open = false;
  scenarioMenu.querySelector("summary").focus({ preventScroll: true });
  renderView();
});
for (const [index, select] of [firstSelect, secondSelect].entries()) {
  select.addEventListener("change", () => setCustomSite(index, select.value));
}
for (const button of customSlotButtons) {
  button.addEventListener("click", () => {
    activeCustomSlot = Number(button.dataset.customSlot);
    customNotice = "";
    renderCustomControls();
  });
}
for (const button of customClearButtons) {
  button.addEventListener("click", () => setCustomSite(Number(button.dataset.clearSlot), ""));
}
for (const button of customPinButtons) {
  button.addEventListener("click", () => {
    const slot = Number(button.dataset.pinSlot);
    pinnedCustomSlot = pinnedCustomSlot === slot ? null : slot;
    customNotice = pinnedCustomSlot === null
      ? "Закрепление снято."
      : `${customPair[slot]} закреплена для подбора.`;
    renderCustomControls();
  });
}
excludeSelect.addEventListener("change", () => {
  const removed = excludeSelect.value ? customPair.indexOf(excludeSelect.value) : -1;
  if (removed >= 0) setCustomSite(removed, "");
  else {
    customNotice = "";
    renderView();
  }
});
element("suggest-custom-pair").addEventListener("click", () => {
  suggestCustomPair();
  renderView();
});
customCompare.addEventListener("change", renderView);
criticalThreshold.addEventListener("change", renderCriticalDecision);
criticalMapButton.addEventListener("click", openCriticalDecision);
stressNormal.addEventListener("click", () => {
  stressScenario = "normal";
  stressManualFailure = "";
  renderView();
});
for (const [index, button] of [stressFirstFailure, stressSecondFailure].entries()) {
  button.addEventListener("click", () => {
    stressScenario = "manual";
    stressManualFailure = model.stress.option.ids[index];
    renderView();
  });
}
stressWorst.addEventListener("click", () => {
  stressScenario = "worst";
  stressManualFailure = "";
  renderView();
});
stressPlanB.addEventListener("click", () => {
  const failed = model.stress?.failed;
  if (failed) openReplacementPlan(model.stress.option.ids.find((id) => id !== failed), failed);
});
radiusControl.addEventListener("input", () => {
  const radius = Number(radiusControl.value);
  if (!planningMode() || (radius !== officialRadius && !data.radius_scenarios?.[radius])) {
    radiusControl.value = currentRadius();
    return;
  }
  if (planningRadius !== radius) {
    planningRadius = radius;
    customNotice = "";
    renderView();
  }
});
exportScenarioButton.addEventListener("click", exportScenario);
radiusDecision.addEventListener("toggle", renderRadiusDecision);
radiusDecisionRange.addEventListener("change", renderRadiusDecision);
element("radius-decision-map").addEventListener("click", () => openRadiusDecision());

element("reset-map").addEventListener("click", () => {
  map.closePopup();
  map.fitBounds(data.bounds, { padding: [21, 21], animate: false });
});
candidateControl.addEventListener("change", renderStations);
heatControl.addEventListener("change", () => {
  renderHeatLayer();
  updateCellStyles();
});
comparisonRadii.addEventListener("change", renderRadii);
for (const button of compareFilters) {
  button.addEventListener("click", () => {
    const category = button.dataset.compareFilter;
    if (highlightedGroups.has(category)) highlightedGroups.delete(category);
    else highlightedGroups.add(category);
    updateCompareFocus();
  });
}

element("dropped-count").textContent = number.format(data.result.dropped_rows);
setExclusionOptions();
map.on("moveend", drawLabels);
renderView();
