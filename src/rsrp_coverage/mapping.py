"""Карта покрытия в одном HTML-файле на Leaflet.

Без сторонних библиотек: модуль собирает готовую HTML-страницу средствами stdlib.
Leaflet подключается с CDN; при отсутствии сети подложка не загрузится,
но клетки, башни и площадки всё равно отрисуются.
"""
from __future__ import annotations

import json
from pathlib import Path

from .geo import cell_center
from .pipeline import Result

COLORS = {"green": "#2e9e4f", "yellow": "#e6b422", "red": "#d1342f"}

TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Карта покрытия</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin: 0; height: 100%; font-family: system-ui, sans-serif; }
  /* Фон виден, когда подложка выключена или недоступна без сети. */
  #map { height: 100%; background: #eef1f4; }
  .panel {
    position: absolute; top: 12px; right: 12px; z-index: 1000;
    background: #fff; padding: 12px 14px; border-radius: 8px;
    box-shadow: 0 2px 12px rgba(0,0,0,.25); font-size: 13px; line-height: 1.5;
    max-width: 280px;
  }
  .panel h2 { margin: 0 0 6px; font-size: 14px; }
  .sw { display: inline-block; width: 12px; height: 12px; margin-right: 6px;
        border-radius: 2px; vertical-align: -1px; }
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h2>Карта покрытия</h2>
  <div><span class="sw" style="background:__GREEN__"></span>RSRP &ge; -90 дБм</div>
  <div><span class="sw" style="background:__YELLOW__"></span>-105 … -90 дБм</div>
  <div><span class="sw" style="background:__RED__"></span>&lt; -105 дБм (слабая)</div>
  <hr>
  <div>Клеток учтено: <b>__CELL_COUNT__</b></div>
  <div>Слабых клеток: <b>__WEAK_COUNT__</b></div>
  <div>Отброшено строк: <b>__DROPPED__</b></div>
  <div>Новые БС: <b>__BEST__</b></div>
  <div>Покрыто слабых: <b>__COVERED__</b></div>
</div>
<script>
var DATA = __PAYLOAD__;
var map = L.map("map").fitBounds([[DATA.bounds[0], DATA.bounds[1]],
                                  [DATA.bounds[2], DATA.bounds[3]]]);

// Подложка нужна только для географического контекста: клетки, вышки и
// радиусы покрытия читаются полностью без неё. Поэтому ни один провайдер
// не является обязательным, а отказ любого из них не ломает страницу.
// Esri Light Gray стоит первым: он не требует ключа и это приглушённая
// серая основа, которая не спорит с красно-жёлто-зелёной заливкой клеток.
var ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/";
var basemaps = {
  "Светло-серая": L.tileLayer(
    ESRI + "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 16, attribution: "&copy; Esri" }),
  "Улицы": L.tileLayer(
    ESRI + "World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "&copy; Esri" }),
  "OpenStreetMap": L.tileLayer(
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "&copy; OpenStreetMap" }),
  "Без подложки": L.tileLayer("")
};

basemaps["Светло-серая"].addTo(map);
L.control.layers(basemaps, null, { collapsed: true }).addTo(map);

// Нет сети или провайдер отказал — снимаем подложку, чтобы поверх данных
// не висели кадры с ошибкой. Остаётся чистый фон, как и задумано.
Object.keys(basemaps).forEach(function (name) {
  var layer = basemaps[name];
  var failures = 0;
  layer.on("tileerror", function () {
    failures += 1;
    if (failures >= 4 && map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
  });
});

DATA.cells.forEach(function (c) {
  L.rectangle([[c.s, c.w], [c.n, c.e]], {
    color: c.color, weight: 1, fillColor: c.color, fillOpacity: 0.45
  }).bindPopup("<b>" + c.id + "</b><br>медиана RSRP: " + c.median +
               " дБм<br>замеров: " + c.n_obs).addTo(map);
});

DATA.worst.forEach(function (w, i) {
  L.marker([w.lat, w.lon]).addTo(map)
   .bindPopup("Худшая клетка #" + (i + 1) + "<br><b>" + w.id + "</b><br>" +
              w.median + " дБм");
});

DATA.towers.forEach(function (t) {
  L.circleMarker([t.lat, t.lon], {
    radius: 7, color: "#1f3c88", fillColor: "#1f3c88", fillOpacity: 0.9
  }).bindPopup("Существующая БС <b>" + t.id + "</b>").addTo(map);
});

DATA.chosen.forEach(function (s) {
  L.circleMarker([s.lat, s.lon], {
    radius: 9, color: "#8e24aa", fillColor: "#ce93d8", fillOpacity: 0.95, weight: 3
  }).bindPopup("Новая БС <b>" + s.id + "</b>").addTo(map);
  L.circle([s.lat, s.lon], {
    radius: 1000, color: "#8e24aa", weight: 2, fill: false, dashArray: "6 6"
  }).addTo(map);
});
</script>
</body>
</html>
"""


def cell_corners(ix: int, iy: int, result: Result) -> tuple[float, float, float, float]:
    """Юг, запад, север, восток клетки в градусах."""
    bounds = result.bounds
    center_lat, center_lon = cell_center(ix, iy, bounds)
    next_lat, next_lon = cell_center(ix + 1, iy + 1, bounds)
    half_lat = (next_lat - center_lat) / 2.0
    half_lon = (next_lon - center_lon) / 2.0
    return (center_lat - half_lat, center_lon - half_lon,
            center_lat + half_lat, center_lon + half_lon)


def build_payload(result: Result) -> dict:
    """Данные для отрисовки: клетки, худшие, существующие БС и выбранные площадки."""
    chosen_ids = set(result.best_candidates)
    payload = {
        "bounds": [result.bounds.lat_min, result.bounds.lon_min,
                   result.bounds.lat_max, result.bounds.lon_max],
        "cells": [],
        "worst": [
            {"id": cell.cell_id, "lat": cell.center_lat, "lon": cell.center_lon,
             "median": cell.rsrp_median}
            for cell in result.worst
        ],
        "towers": [{"id": s.id, "lat": s.lat, "lon": s.lon} for s in result.towers],
        "chosen": [{"id": s.id, "lat": s.lat, "lon": s.lon}
                   for s in result.candidates if s.id in chosen_ids],
    }
    for cell in result.cells:
        south, west, north, east = cell_corners(cell.ix, cell.iy, result)
        payload["cells"].append({
            "id": cell.cell_id, "s": south, "w": west, "n": north, "e": east,
            "color": COLORS[cell.color], "median": cell.rsrp_median,
            "n_obs": cell.sample_count,
        })
    return payload


def render_map(result: Result, output_path: str | Path) -> Path:
    """Собрать HTML-карту и записать её на диск."""
    replacements = {
        "__GREEN__": COLORS["green"],
        "__YELLOW__": COLORS["yellow"],
        "__RED__": COLORS["red"],
        "__CELL_COUNT__": str(len(result.cells)),
        "__WEAK_COUNT__": str(len(result.weak)),
        "__DROPPED__": str(result.report.dropped_rows),
        "__BEST__": ", ".join(result.best_candidates),
        "__COVERED__": str(result.covered_weak_cells),
        "__PAYLOAD__": json.dumps(build_payload(result), ensure_ascii=False),
    }
    html = TEMPLATE
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target
