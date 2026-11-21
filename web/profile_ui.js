"use strict";

function createBuildingInspector({ map, readScene, readRadio, sites, layers, baselinePair, number }) {
  const panel = document.getElementById("buildings-panel");
  const legend = document.getElementById("buildings-legend");
  const siteSelect = document.getElementById("building-site");
  const cellSelect = document.getElementById("building-cell");
  const measurement = document.getElementById("building-measurement");
  const measuredRsrp = document.getElementById("building-rsrp");
  const sampleCount = document.getElementById("building-samples");
  const routeLabel = document.getElementById("building-route");
  const heightInput = document.getElementById("building-height");
  const heightValue = document.getElementById("building-height-value");
  const heightPreset = document.getElementById("building-height-preset");
  const verdict = document.getElementById("building-verdict");
  const evidence = document.getElementById("building-evidence");
  const loss = document.getElementById("building-loss");
  const roofLoss = document.getElementById("building-roof-loss");
  const breakdown = document.getElementById("building-loss-breakdown");
  const frequency = document.getElementById("building-frequency");
  const focusButton = document.getElementById("building-focus");
  const chart = document.getElementById("building-chart");
  const counts = document.getElementById("building-counts");
  const heading = document.getElementById("buildings-heading");
  const single = document.getElementById("building-single");
  const singleSelect = document.getElementById("building-single-select");
  const pairDetails = document.getElementById("building-pairs");
  const pairRows = document.getElementById("building-pair-rows");
  const pairCase = document.getElementById("building-pair-case");
  const pairLegend = document.getElementById("buildings-pair-legend");
  const gapToggle = document.getElementById("building-show-gaps");
  const roofToggle = document.getElementById("building-show-rooftop");
  const roofPanel = document.getElementById("building-rooftop-panel");
  const studyPicker = document.getElementById("building-study-picker");
  const studyCell = document.getElementById("building-study-cell");
  const uncertainLegend = document.getElementById("building-study-uncertain");
  const profileOptions = document.getElementById("building-options");
  const gapPanel = document.getElementById("building-gap-panel");
  const gapLegend = document.getElementById("buildings-gap-legend");
  const gapSelect = document.getElementById("building-gap-cell");
  const gapDetail = document.getElementById("building-gap-detail");
  const style = getComputedStyle(document.documentElement);
  const colors = Object.fromEntries(["line", "clear", "blocked", "unknown"].map((name) =>
    [name, style.getPropertyValue("--profile-" + name).trim()],
  ));
  const initialHeight = 25;
  const alternativeHeight = 40;
  const pane = map.createPane("building-profile");
  pane.style.zIndex = "430";
  pane.style.pointerEvents = "none";
  const overlay = L.layerGroup().addTo(map);
  const availableSites = new Map(sites.map((site) => [site.id, site]));

  let radio;
  let buildings;
  let previousView;
  let visible = false;
  let period;
  let selectedCellId = "";
  let weakCells = new Map();
  let mappedCase = "";
  let comparisonPoints = [];
  let showingGaps = false;
  let showingRooftop = false;

  for (const site of sites) siteSelect.add(new Option(site.id, site.id));
  siteSelect.value = availableSites.has(baselinePair[0]) ? baselinePair[0] : sites[0]?.id ?? "";
  siteSelect.disabled = !sites.length;

  function cellCenter(cell) {
    const [[south, west], [north, east]] = cell.bounds;
    return [(south + north) / 2, (west + east) / 2];
  }

  function endpoints(cell = weakCells.get(selectedCellId)) {
    const site = availableSites.get(siteSelect.value);
    return site && cell ? [[site.lat, site.lon], cellCenter(cell)] : null;
  }

  function closestSite(cell, preferredIds) {
    const target = cellCenter(cell);
    const preferred = sites.filter((site) => preferredIds.includes(site.id) &&
      site.covered_by_period[period].includes(cell.id));
    const options = preferred.length ? preferred : sites;
    const ranked = options.map((site) => ({ site, distance: map.distance([site.lat, site.lon], target) }));
    return ranked.sort((a, b) => a.distance - b.distance || a.site.id.localeCompare(b.site.id))[0]?.site;
  }

  function defaultCell() {
    return [...weakCells.values()]
      .map((cell) => {
        const points = endpoints(cell);
        return { id: cell.id, distance: points ? map.distance(...points) : 0 };
      })
      .sort((a, b) => a.distance - b.distance || a.id.localeCompare(b.id))[0]?.id ?? "";
  }

  function choosePeriod(nextPeriod, requestedCellId) {
    const firstVisit = period === undefined;
    period = nextPeriod;
    if (period !== "all") showingRooftop = false;
    weakCells = new Map(layers[period].filter((cell) => cell.state === "weak").map((cell) => [cell.id, cell]));
    cellSelect.replaceChildren(...[...weakCells.values()].map((cell) =>
      new Option(cell.id + " · " + number.format(cell.median) + " дБм", cell.id),
    ));
    const [exampleSite, exampleCell] = firstVisit && !weakCells.has(requestedCellId) ? radio.example ?? [] : [];
    if (weakCells.has(exampleCell)) {
      siteSelect.value = exampleSite;
      selectedCellId = exampleCell;
    } else if (weakCells.has(requestedCellId)) {
      selectedCellId = requestedCellId;
    } else if (!weakCells.has(selectedCellId)) {
      selectedCellId = defaultCell();
    }
    cellSelect.value = selectedCellId;
    cellSelect.disabled = !weakCells.size;
  }

  function crossingState({ roof, blocked }) {
    return roof === null ? "unknown" : blocked ? "blocked" : "clear";
  }

  function crossedBuildings(crossings) {
    const footprints = new Map();
    for (const crossing of crossings) {
      const state = crossingState(crossing);
      const id = crossing.building.id;
      if (!footprints.has(id) || state === "blocked") {
        footprints.set(id, { building: crossing.building, state });
      }
    }
    return footprints;
  }

  function drawFootprint(building, state, options) {
    const geometry = Array.isArray(building.polygons)
      ? { type: "MultiPolygon", coordinates: building.polygons }
      : building.polygons;
    L.geoJSON(geometry, {
      ...options,
      style: { color: colors[state], fillColor: colors[state], fillOpacity: state === "unknown" ? 0 : .46,
        weight: 1.6, dashArray: state === "unknown" ? "4 3" : null },
    }).addTo(overlay);
  }

  function comparisonMarker(point, text, color, options, direction = "top") {
    const label = document.createElement("span");
    label.textContent = text;
    L.circleMarker(point, { ...options, pane: "station", radius: 5, color,
      weight: 2, fillColor: "#182327", fillOpacity: 1 }).bindTooltip(label, {
      pane: "station", permanent: true, direction, className: "station-label",
      offset: [0, direction === "top" ? -6 : 4],
    }).addTo(overlay);
  }

  function renderOverlay(points, footprints) {
    overlay.clearLayers();
    if (!points) return;
    const options = { pane: "building-profile", interactive: false };

    L.rectangle(weakCells.get(selectedCellId).bounds, {
      ...options, color: colors.line, fill: false, opacity: .85, weight: 1.5,
    }).addTo(overlay);

    for (const { building, state } of footprints.values()) {
      drawFootprint(building, state, options);
    }

    L.polyline(points, { ...options, color: colors.line, opacity: .94, weight: 2 }).addTo(overlay);
    for (const [i, point] of points.entries()) {
      const label = document.createElement("span");
      label.textContent = i === 0 ? siteSelect.value : selectedCellId;
      L.circleMarker(point, {
        ...options, pane: "station", radius: i === 0 ? 5.5 : 4, color: colors.line, fillColor: "#182327",
        weight: 2, fillOpacity: 1,
      }).bindTooltip(label, {
        pane: "station", direction: i === 0 ? "top" : "bottom", offset: [0, i === 0 ? -6 : 5],
        permanent: true, className: "station-label",
      }).addTo(overlay);
    }
  }

  function svg(tag, attributes, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderChart(profile, height) {
    const { distance, crossings, ground, targetHeight } = profile;
    const levels = [0, height, targetHeight, ...ground.map((dm) => dm / 10),
      ...crossings.flatMap(({ base, roof }) => [base, roof ?? base])];
    const minAxis = Math.floor(Math.min(...levels) / 20) * 20;
    const maxAxis = Math.max(minAxis + 20, Math.ceil(Math.max(...levels) / 20) * 20);
    const x = (metres) => 33 + 295 * Math.max(0, Math.min(1, metres / distance));
    const y = (metres) => 94 - 77 * (metres - minAxis) / (maxAxis - minAxis);

    const nodes = [svg("line", { x1: x(0), x2: x(0), y1: y(maxAxis), y2: y(minAxis), class: "profile-axis" })];
    for (const level of [minAxis, (minAxis + maxAxis) / 2, maxAxis]) {
      nodes.push(svg("line", { x1: x(0), x2: x(distance), y1: y(level), y2: y(level),
        class: level === 0 ? "profile-axis" : "profile-guide" }));
      nodes.push(svg("text", { x: 28, y: y(level) + 3, "text-anchor": "end" }, number.format(level) + " м"));
    }

    for (const crossing of crossings) {
      const { building, start, end, base, roof: top } = crossing;
      const state = crossingState(crossing);
      const rect = svg("rect", {
        x: x(start), width: Math.max(1, x(end) - x(start)),
        y: top === null ? y(base) - 5 : y(top),
        height: top === null ? 5 : Math.max(1, y(base) - y(top)),
        class: "profile-roof is-building-" + state,
      });
      const source = building.height_source === "levels" ? " · оценка по этажности" : "";
      rect.append(svg("title", {}, top === null ? "Высота неизвестна" : number.format(building.height) + " м над землёй" + source));
      nodes.push(rect);
    }

    nodes.push(svg("polyline", {
      points: ground.map((dm, i) => x(distance * i / (ground.length - 1)) + "," + y(dm / 10)).join(" "),
      class: "profile-ground",
    }));
    nodes.push(svg("line", { x1: x(0), y1: y(height), x2: x(distance), y2: y(targetHeight), class: "profile-ray" }));
    nodes.push(svg("circle", { cx: x(0), cy: y(height), r: 2.4, class: "profile-point" }));
    nodes.push(svg("circle", { cx: x(distance), cy: y(targetHeight), r: 2.4, class: "profile-point" }));
    nodes.push(svg("text", { x: x(0), y: 110 }, "Антенна · 0 м"));
    nodes.push(svg("text", { x: x(distance), y: 110, "text-anchor": "end" }, "Клетка · " + number.format(Math.round(distance)) + " м"));
    chart.replaceChildren(...nodes);
    chart.setAttribute("aria-label", "Профиль рельефа и крыш по трассе длиной " +
      number.format(Math.round(distance)) + " м. Высоты относительно земли у антенны.");
  }

  function renderProfile() {
    const points = endpoints();
    const height = Number(heightInput.value);
    const heightIndex = radio.heights?.indexOf(height) ?? -1;
    const profile = radio.routes?.[siteSelect.value]?.[selectedCellId];
    const usable = !!profile?.distance;
    const crossings = (profile?.crossings ?? []).map(([id, start, end, base, roof, mask]) => ({
      building: buildings.get(id), start, end, base, roof, blocked: !!(mask & (1 << heightIndex)),
    }));
    const footprints = crossedBuildings(crossings);
    const cell = weakCells.get(selectedCellId);
    const distance = usable ? " · " + number.format(Math.round(profile.distance)) + " м" : "";
    routeLabel.textContent = points ? siteSelect.value + " → " + selectedCellId + distance : "";
    measurement.hidden = !cell;
    if (cell) {
      measuredRsrp.textContent = number.format(cell.median).replace("-", "−") + " дБм";
      sampleCount.textContent = number.format(cell.count);
    }

    const estimate = profile?.losses?.[frequency.value]?.[heightIndex];
    loss.hidden = !estimate;
    if (estimate) {
      const [terrain, combined, roofs] = estimate;
      roofLoss.textContent = (roofs > 0 ? "+" : "") + number.format(roofs) + " дБ";
      breakdown.textContent = "Рельеф " + number.format(terrain) + " дБ · С крышами " +
        number.format(combined) + " дБ · " + number.format(Number(frequency.value)) + " МГц";
    } else {
      roofLoss.textContent = "";
      breakdown.textContent = "";
    }

    const states = [...footprints.values()];
    const blocked = states.filter(({ state }) => state === "blocked").length;
    const unknown = states.filter(({ state }) => state === "unknown").length;
    const messages = {
      outside: "Трасса выходит за границы данных о рельефе или зданиях",
      coincident: "Площадка совпадает с центром клетки",
    };
    const notes = [];
    let status = messages[profile?.status] ??
      (estimate ? "" : points ? "Расчёт трассы недоступен" : "Выберите слабую клетку и площадку");
    if (profile?.status === "endpoint") {
      status = profile.siteIndoors
        ? "Площадка " + siteSelect.value + " внутри контура здания"
        : "Центр клетки внутри контура здания";
      if (profile.siteIndoors && profile.targetIndoors) status = "Обе точки внутри контуров зданий";
      notes.push("Для расчёта нужны точное положение антенны или приёмника и параметры здания.");
    }
    verdict.textContent = status;
    if (estimate && profile.partial) notes.push("Оценка частичная · пропущено зданий " + number.format(profile.partial) + ".");
    if (estimate && !profile.partial && states.some(({ building }) => building.height_source === "levels")) {
      notes.push("Высоты крыш оценены по этажности.");
    }
    if (usable && !availableSites.get(siteSelect.value).covered_by_period[period].includes(selectedCellId)) {
      notes.push("Клетка дальше 1 км.");
    }
    evidence.textContent = notes.join(" ");
    document.getElementById("building-status").hidden = !verdict.textContent && !notes.length;
    heightValue.textContent = number.format(height) + " м";
    heightInput.disabled = !usable;
    heightPreset.disabled = !usable;
    heightPreset.textContent = height >= alternativeHeight ? "Вернуть 25 м" : "Поднять до 40 м";
    chart.toggleAttribute("hidden", !usable);
    counts.hidden = !usable;
    focusButton.disabled = !points;
    legend.hidden = !points;
    renderOverlay(points, footprints);
    if (!usable) return;
    document.getElementById("building-crossings").textContent = number.format(footprints.size);
    document.getElementById("building-blocked").textContent = number.format(blocked);
    document.getElementById("building-unknown").textContent = number.format(unknown);
    renderChart({ ...profile, crossings }, height);
  }

  function renderPairComparison() {
    const height = Number(heightInput.value);
    const mhz = Number(frequency.value);
    const i = radio.heights?.indexOf(height);
    const report = radio.pairs?.[period];
    const example = report?.scenarios?.[mhz]?.[i];
    const official = report?.official;
    const officialName = baselinePair.join(" + ");
    const officialLine = official ?
      officialName + " · оценено " + number.format(official.assessed) + " из " + number.format(official.inRadius) +
        " клеток пары по ТЗ. " : "";
    const conclusion = example?.regional
      ? "В модели для этих двух пар добавление крыш меняет выбор."
      : official && official.assessed === official.inRadius
        ? "Смена пары по ТЗ по всему району не подтверждена."
        : "Данных для замены пары по всему району недостаточно.";
    document.getElementById("building-official-evidence").textContent = officialLine + conclusion +
      (example ? "" : " На общих трассах смена выбора при этих настройках не выявлена.");
    gapToggle.hidden = !official?.inRadius;
    gapToggle.setAttribute("aria-pressed", String(showingGaps));
    const studies = period === "all" ? radio.roofStudies ?? {} : {};
    const studyIds = Object.keys(studies);
    const activeStudy = studies[studyCell.value] ?? studies[studyIds[0]];
    roofToggle.hidden = !activeStudy;
    roofToggle.setAttribute("aria-pressed", String(showingRooftop));
    routeLabel.textContent = number.format(mhz) + " МГц · " + number.format(height) + " м";
    heightInput.disabled = !radio.heights?.length;
    roofPanel.hidden = !showingRooftop || !activeStudy;
    uncertainLegend.hidden = true;
    if (showingRooftop && activeStudy) {
      studyPicker.hidden = studyIds.length < 2;
      studyCell.replaceChildren(...studyIds.map((id) => new Option(id, id)));
      studyCell.value = activeStudy.cell;
      document.getElementById("building-official-evidence").textContent =
        "Пара по ТЗ · " + officialName + " · " + number.format(official.inRadius) + " клеток в 1 км.";
      pairCase.hidden = true;
      gapPanel.hidden = true;
      gapLegend.hidden = true;
      renderRooftop(activeStudy);
      return;
    }
    if (showingGaps) {
      pairCase.hidden = true;
      pairLegend.hidden = true;
      renderDataGaps(report?.dataAudit ?? {});
      return;
    }
    gapPanel.hidden = true;
    gapLegend.hidden = true;
    pairCase.hidden = !example;
    overlay.clearLayers();
    pairLegend.hidden = !example;
    comparisonPoints = [];
    if (!example) {
      mappedCase = "";
      return;
    }
    const { reference, alternative, assessed, commonRadius, focus } = example;
    document.getElementById("building-pair-ref").textContent = reference.join(" + ");
    document.getElementById("building-pair-alt").textContent = alternative.join(" + ");
    document.getElementById("building-pair-result").textContent = example.regional
      ? "После учёта крыш проходит больше клеток у " + alternative.join(" + ")
      : "На общих проверенных клетках выигрывает " + alternative.join(" + ");
    document.getElementById("building-pair-scope").textContent =
      "Лимит дифракции " + radio.limit + " дБ. Проверены " + number.format(assessed) +
      " из " + number.format(commonRadius) + " общих клеток в радиусе 1 км." +
      (example.regional ? " По всем клеткам двух пар после учёта крыш · " +
        example.regional.roofs.map((n) => number.format(n)).join(" → ") + "." : "");
    pairRows.replaceChildren();
    for (const [label, scores] of [["Только рельеф", example.terrain], ["С крышами", example.roofs]]) {
      const row = pairRows.insertRow();
      const th = document.createElement("th");
      th.scope = "row";
      th.textContent = label;
      row.append(th);
      for (const n of scores) row.insertCell().textContent = number.format(n) + " из " + number.format(assessed);
    }
    const cell = weakCells.get(focus.cell);
    document.getElementById("building-witness-cell").textContent =
      focus.cell + " · " + number.format(cell.median).replace("-", "−") + " дБм по замерам";
    const target = cellCenter(cell);
    const options = { pane: "building-profile", interactive: false };
    for (const [changed, color] of [[example.gained, colors.clear], [example.lost, colors.blocked]]) {
      for (const id of changed) {
        if (id === focus.cell) continue;
        L.rectangle(weakCells.get(id).bounds, { ...options, color, weight: 2, fillOpacity: .12 }).addTo(overlay);
      }
    }
    L.rectangle(cell.bounds, { ...options, color: colors.line, weight: 2, fillOpacity: .12 }).addTo(overlay);
    const obstructing = new Map();
    const witnessRows = [];
    comparisonPoints = [target];
    for (const [path, state] of [[focus.reference, "blocked"], [focus.alternative, "clear"]]) {
      const site = availableSites.get(path.site);
      const point = [site.lat, site.lon];
      const route = radio.routes[path.site][focus.cell];
      comparisonPoints.push(point);
      if (state === "blocked") {
        for (const [id, , , , , mask] of route.crossings) {
          if (mask & (1 << i)) obstructing.set(id, buildings.get(id));
        }
        for (const building of obstructing.values()) drawFootprint(building, state, options);
      }
      L.polyline([point, target], { ...options, color: colors[state], weight: 3,
        dashArray: state === "blocked" ? "7 5" : null }).addTo(overlay);
      comparisonMarker(point, path.site, colors[state], options);
      const row = document.createElement("li");
      const line = document.createElement("i");
      line.className = "profile-line is-pair-" + (state === "blocked" ? "ref" : "alt");
      line.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.textContent = path.site + " · " + number.format(Math.round(route.distance)) + " м";
      const reading = document.createElement("strong");
      reading.textContent = number.format(path.terrain) + " → " + number.format(path.withRoofs) + " дБ";
      row.append(line, name, reading);
      witnessRows.push(row);
    }
    comparisonMarker(target, focus.cell, colors.line, options, "bottom");
    document.getElementById("building-witness-paths").replaceChildren(...witnessRows);
    const roofs = [...obstructing.values()];
    const first = roofs[0];
    const roofEvidence = roofs.length === 1
      ? "На трассе " + focus.reference.site + " прямую перекрывает крыша " +
        (first.height_source === "levels" ? "около " : "") + number.format(first.height) + " м."
      : roofs.length > 1
        ? "Красным выделены здания, пересекающие прямую от " + focus.reference.site + "."
        : "Разница видна в потерях дифракции вдоль трасс.";
    const shared = reference.find((site) => alternative.includes(site));
    const beyond = !availableSites.get(shared).covered_by_period[period].includes(focus.cell);
    document.getElementById("building-witness-roof").textContent =
      roofEvidence + (beyond ? " Общая площадка " + shared + " дальше 1 км от этой клетки." : "");
    const witnessKey = [focus.cell, focus.reference.site, focus.alternative.site].join("/");
    if (witnessKey !== mappedCase) focusComparison();
    mappedCase = witnessKey;
  }

  function focusComparison() {
    if (comparisonPoints.length) {
      map.fitBounds(L.latLngBounds(comparisonPoints), { padding: [35, 35], maxZoom: 17, animate: false });
    }
  }

  function renderRooftop(study) {
    const mhz = 1800;
    const index = radio.heights.indexOf(initialHeight);
    const cell = weakCells.get(study.cell);
    const target = cellCenter(cell);
    const byPair = [study.official, study.alternative];
    const options = { pane: "building-profile", interactive: false };
    const rows = document.getElementById("building-rooftop-rows");
    overlay.clearLayers();
    rows.replaceChildren();
    pairLegend.hidden = false;
    comparisonPoints = [target];
    L.rectangle(cell.bounds, { ...options, color: colors.line, weight: 2, fillOpacity: .10 }).addTo(overlay);

    const passed = [];
    const uncertain = [];
    const evidence = [];
    for (const pair of byPair) {
      const id = pair.find((site) => Object.hasOwn(study.sites, site));
      const route = study.sites[id];
      const [ground, total] = route.losses[mhz][index];
      const ranges = study.spread[id][mhz];
      const [low, high] = ranges[index];
      const state = high <= radio.limit ? "clear" : low > radio.limit ? "blocked" : "line";
      const site = availableSites.get(id);
      const point = [site.lat, site.lon];
      comparisonPoints.push(point);
      if (state === "clear") passed.push(pair.join(" + "));
      if (state === "line") {
        uncertain.push(id);
        const tested = radio.heights.find((height, i) =>
          height >= initialHeight && ranges.slice(i).every((range) => range && range[1] <= radio.limit),
        );
        if (tested) evidence.push("От " + tested + " м у " + id + " все проверенные варианты в лимите.");
      }
      for (const [buildingId, , , , , mask] of route.crossings) {
        if (mask & (1 << index)) {
          drawFootprint(buildings.get(buildingId), "blocked", options);
        } else if (state === "line" && study.sources[buildingId]?.alternateFloors > 0) {
          drawFootprint(buildings.get(buildingId), "line", options);
        }
      }
      L.polyline([point, target], { ...options, color: colors[state], weight: 3,
        dashArray: state === "blocked" ? "7 5" : state === "line" ? "3 4" : null }).addTo(overlay);
      comparisonMarker(point, id, colors[state], options);
      const row = rows.insertRow();
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = pair.join(" + ") + " · " + id + ", " + number.format(Math.round(route.distance)) + " м";
      row.append(name);
      row.insertCell().textContent = number.format(ground) + " → " + number.format(total) + " дБ";
      row.insertCell().textContent = (low === high ? number.format(low) :
        number.format(low) + "–" + number.format(high)) + " дБ";
    }
    uncertainLegend.hidden = !uncertain.length;
    comparisonMarker(target, study.cell, colors.line, options, "bottom");
    routeLabel.textContent = number.format(mhz) + " МГц · " + initialHeight + " м над землёй";
    document.getElementById("building-rooftop-result").textContent =
      study.cell + " · " + (passed.length === 1 ? "в лимите " + passed[0] :
        passed.length === 2 ? "обе пары в лимите" : uncertain.length ?
          "для " + uncertain.join(" и ") + " вывод зависит от высот" : "обе пары выше лимита");
    document.getElementById("building-rooftop-signal").textContent = "Медиана замеров " +
      number.format(cell.median).replace("-", "−") + " дБм · лимит дифракции " + radio.limit + " дБ";

    const sources = document.getElementById("building-rooftop-sources");
    sources.replaceChildren(...Object.values(study.sources).map((source) => {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = source.link;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = source.name;
      li.append(link, " · этажей в модели " + source.floors + " · ≈" +
        number.format(source.floors * study.storeyMetres[1]) + " м" +
        (source.note ? ". " + source.note : source.alternateFloors ?
          " (в OSM " + source.alternateFloors + " этажа)" : ""));
      return li;
    }));
    const rooftop = study.sites[study.rooftopSite];
    const last = number.format(rooftop.losses[mhz].at(-1)[1]);
    const closestRoof = Math.max(0, ...rooftop.crossings.filter(([, , , , , mask]) => mask & (1 << index))
      .map(([, , end]) => end));
    document.getElementById("building-rooftop-note").textContent =
      "Разброс по сценариям с " + number.format(study.storeyMetres[0]) + "–" +
      number.format(study.storeyMetres.at(-1)) + " м на этаж и вариантами для спорных зданий. " +
      (evidence.length ? evidence.join(" ") + " " : "") +
      "У " + study.rooftopSite + " при " + radio.heights.at(-1) + " м остаётся " + last + " дБ." +
      (closestRoof ? " Ближайшая крыша, пересекающая прямую, заканчивается за " +
        number.format(Math.round(rooftop.distance - closestRoof)) + " м до центра клетки." : "");
    const key = "roof/" + study.cell;
    if (key !== mappedCase) focusComparison();
    mappedCase = key;
  }

  function renderDataGaps(audit) {
    const labels = {
      heights: "Нужны высоты крыш", receiver: "Центр клетки в контуре",
      site: "Площадка в контуре", coincident: "Точки совпали",
      outside: "Вне вырезки", ready: "Трасса рассчитана",
    };
    const palette = {
      heights: colors.line, receiver: colors.blocked, site: colors.blocked,
      coincident: colors.unknown, outside: colors.unknown, ready: colors.clear,
    };
    const entries = Object.entries(audit);
    const previous = gapSelect.value;
    gapPanel.hidden = false;
    gapLegend.hidden = !entries.length;
    overlay.clearLayers();
    gapSelect.replaceChildren(new Option("Все клетки", ""), ...entries.map(([cell]) => new Option(cell, cell)));
    gapSelect.value = Object.hasOwn(audit, previous) ? previous : "";
    const chosen = gapSelect.value;
    const mapBounds = L.latLngBounds([]);
    const tally = new Map();
    const options = { pane: "building-profile", interactive: false };
    for (const [id, paths] of entries) {
      const [, cause] = paths[0];
      const { bounds } = weakCells.get(id);
      tally.set(cause, (tally.get(cause) ?? 0) + 1);
      mapBounds.extend(bounds);
      L.rectangle(bounds, { ...options, color: palette[cause],
        weight: chosen === id ? 3 : 1.25, fillOpacity: chosen === id ? .32 : .16,
      }).addTo(overlay);
    }
    const countsList = document.getElementById("building-gap-counts");
    countsList.replaceChildren(...Object.entries(labels).filter(([cause]) => tally.has(cause)).map(([cause, label]) => {
      const item = document.createElement("li");
      const count = document.createElement("strong");
      count.textContent = number.format(tally.get(cause));
      item.append(label, count);
      return item;
    }));
    for (const id of baselinePair) {
      const site = availableSites.get(id);
      if (!site) continue;
      const point = [site.lat, site.lon];
      mapBounds.extend(point);
      const label = document.createElement("span");
      label.textContent = id;
      L.circleMarker(point, { ...options, pane: "station", radius: 4, color: colors.line })
        .bindTooltip(label, { pane: "station", permanent: true, direction: "top", className: "station-label" })
        .addTo(overlay);
    }
    const details = [];
    const focus = L.latLngBounds([]);
    if (chosen) {
      const target = cellCenter(weakCells.get(chosen));
      focus.extend(weakCells.get(chosen).bounds);
      for (const [id, cause] of audit[chosen]) {
        const site = availableSites.get(id);
        const point = [site.lat, site.lon];
        focus.extend(point);
        L.polyline([point, target], { ...options, color: palette[cause], weight: 2.8 }).addTo(overlay);
        const route = radio.routes[id][chosen];
        const affected = new Set();
        for (const [buildingId, start, end, , roof] of route.crossings ?? []) {
          const building = buildings.get(buildingId);
          if (cause === "site" && start === 0 || cause === "receiver" && end === route.distance ||
              cause === "heights" && (roof === null || building.base > 0)) affected.add(buildingId);
        }
        for (const buildingId of affected) {
          drawFootprint(buildings.get(buildingId), cause === "heights" ? "unknown" : "blocked", options);
        }
        const listed = [...affected].slice(0, 4).join(", ");
        details.push(id + " · " + labels[cause] + (listed ? " (" + listed + (affected.size > 4 ? " и др." : "") + ")" : "") + ".");
      }
    }
    gapDetail.textContent = details.join(" ") || "Выберите клетку, чтобы увидеть трассу и нужные контуры.";
    const key = "gap/" + period + "/" + chosen;
    const bounds = chosen ? focus : mapBounds;
    if (mappedCase !== key && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [38, 38], maxZoom: 17, animate: false });
    }
    mappedCase = key;
  }

  function renderHeight() {
    if (!visible) return;
    const compare = pairDetails.open;
    profileOptions.hidden = compare && showingRooftop;
    heading.textContent = compare ? "Здания и выбор пары" : "Проверка трассы";
    single.hidden = compare;
    singleSelect.hidden = compare;
    focusButton.hidden = compare;
    if (compare) {
      legend.hidden = true;
      counts.hidden = true;
      renderPairComparison();
    } else {
      mappedCase = "";
      pairLegend.hidden = true;
      gapLegend.hidden = true;
      renderProfile();
    }
  }

  function focusPath() {
    const points = endpoints();
    if (points) map.fitBounds(L.latLngBounds(points), { padding: [46, 46], maxZoom: 17, animate: false });
  }

  function selectCell(id, focus = false) {
    if (!visible || !weakCells.has(id)) return;
    if (pairDetails.open) {
      if (showingGaps && radio.pairs?.[period]?.dataAudit?.[id]) {
        gapSelect.value = id;
        renderPairComparison();
      }
      return;
    }
    selectedCellId = id;
    cellSelect.value = id;
    renderProfile();
    if (focus) focusPath();
  }

  function render(show, nextPeriod, requestedCellId, preferredSites = []) {
    panel.hidden = !show;
    if (!show) {
      if (visible) {
        overlay.clearLayers();
        legend.hidden = true;
        pairLegend.hidden = true;
        gapLegend.hidden = true;
        mappedCase = "";
        map.setView(previousView.center, previousView.zoom, { animate: false });
      }
      visible = false;
      return;
    }

    radio ??= readRadio();
    buildings ??= new Map(readScene().buildings.map((building) => [building.id, building]));
    const entering = !visible;
    if (entering) previousView = { center: map.getCenter(), zoom: map.getZoom() };
    const periodChanged = period !== nextPeriod;
    const previousCell = selectedCellId;
    visible = true;
    if (periodChanged) choosePeriod(nextPeriod, requestedCellId);
    const requestedCell = weakCells.get(requestedCellId);
    if (requestedCell) {
      pairDetails.open = false;
      selectedCellId = requestedCell.id;
      cellSelect.value = selectedCellId;
      siteSelect.value = closestSite(requestedCell, preferredSites)?.id ?? "";
    }
    renderHeight();
    if (!pairDetails.open && (entering || requestedCell || (periodChanged && selectedCellId !== previousCell))) focusPath();
  }

  siteSelect.addEventListener("change", () => {
    if (!weakCells.has(selectedCellId)) selectedCellId = defaultCell();
    cellSelect.value = selectedCellId;
    renderProfile();
    focusPath();
  });
  cellSelect.addEventListener("change", () => selectCell(cellSelect.value, true));
  heightInput.addEventListener("input", renderHeight);
  frequency.addEventListener("change", renderHeight);
  heightPreset.addEventListener("click", () => {
    heightInput.value = Number(heightInput.value) >= alternativeHeight ? initialHeight : alternativeHeight;
    renderHeight();
  });
  pairDetails.addEventListener("toggle", () => {
    renderHeight();
    if (visible && !pairDetails.open) focusPath();
  });
  gapToggle.addEventListener("click", () => {
    showingGaps = !showingGaps;
    showingRooftop = false;
    renderHeight();
  });
  roofToggle.addEventListener("click", () => {
    showingRooftop = !showingRooftop;
    showingGaps = false;
    renderHeight();
  });
  studyCell.addEventListener("change", renderHeight);
  gapSelect.addEventListener("change", renderHeight);
  for (const id of ["building-witness-focus", "building-rooftop-focus"]) {
    document.getElementById(id).addEventListener("click", () => {
      focusComparison();
      map.getContainer().scrollIntoView({ block: "nearest" });
    });
  }
  focusButton.addEventListener("click", () => {
    focusPath();
    map.getContainer().scrollIntoView({ block: "nearest" });
  });

  return { render, selectCell };
}
