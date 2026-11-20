"""Проверки HTML-карты: шаблон, геометрия клеток и тепловой слой.

Карта — это продукт, который смотрит жюри, поэтому здесь проверяются именно
правила отрисовки, а не внутренности шаблона: страница собирается целиком,
в ней не остаётся незаменённых плейсхолдеров, прямоугольник клетки стоит
ровно на линиях сетки, а тепловой слой нормирован по фактическим данным
и подключён оверлеем, то есть по умолчанию выключен.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from rsrp_coverage.config import (MIN_MEASUREMENTS_PER_CELL, RSRP_GREEN_MIN,
                                  RSRP_YELLOW_MIN)
from rsrp_coverage.geo import cell_center, project_m
from rsrp_coverage.mapping import (HEAT_GRADIENT, build_heat, build_payload, cell_corners,
                                  cell_step, heat_intensity, render_map)
from rsrp_coverage.pipeline import run

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "07_coverage"
pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "measurements.csv").exists(),
    reason="нет открытого набора данных",
)

# Плейсхолдеры шаблона: __ИМЯ__ заглавными, латиница или кириллица.
PLACEHOLDER = re.compile(r"__[A-ZА-ЯЁ][A-ZА-ЯЁ0-9_]*__")

HEAT_LAYER_NAME = "Тепловая карта сигнала"


@pytest.fixture(scope="module")
def result():
    return run(DATA_DIR)


@pytest.fixture(scope="module")
def payload(result):
    return build_payload(result)


@pytest.fixture(scope="module")
def html(result, tmp_path_factory):
    """Собранная страница — ровно та, что уходит в out/map.html."""
    target = render_map(result, tmp_path_factory.mktemp("map") / "map.html")
    return target.read_text(encoding="utf-8")


def test_render_map_writes_the_file(result, tmp_path):
    """Карта пишется по указанному пути, каталог создаётся сам."""
    target = render_map(result, tmp_path / "вложенный" / "map.html")
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert text.rstrip().endswith("</html>")
    assert len(text) > 10_000


def test_no_unreplaced_placeholders(html):
    """Ни один __ИМЯ__ не должен уехать в готовую страницу."""
    assert PLACEHOLDER.findall(html) == []


def test_payload_is_valid_json_inside_the_page(html, payload):
    """DATA в странице — валидный JSON и совпадает с payload по составу."""
    match = re.search(r"var DATA = (\{.*?\});\n", html, re.DOTALL)
    assert match is not None
    embedded = json.loads(match.group(1))
    assert set(embedded) == set(payload)
    assert len(embedded["cells"]) == len(payload["cells"])
    assert len(embedded["heat"]["points"]) == len(payload["heat"]["points"])


def test_cell_count_matches_accounted_cells(result, payload, html):
    """Клеток в payload ровно столько, сколько учтено пайплайном."""
    assert len(payload["cells"]) == len(result.cells)
    assert {c["id"] for c in payload["cells"]} == {c.cell_id for c in result.cells}
    # Учтённая клетка — это клетка с >= 5 замерами, а не любая с замерами.
    assert all(c["n_obs"] >= MIN_MEASUREMENTS_PER_CELL for c in payload["cells"])
    assert len(payload["cells"]) < result.cells_with_measurements
    assert f"Клеток учтено: <b>{len(result.cells)}</b>" in html


def test_cell_corners_sit_exactly_on_grid_lines(result):
    """Углы клетки обязаны лежать на линиях сетки, без сдвига на полклетки.

    Обратная проекция углов даёт ровно кратные размеру клетки координаты:
    юго-западный угол — (ix * 200, iy * 200), северо-восточный — следующий.
    Любой сдвиг прямоугольника относительно клетки ломает это равенство.
    """
    size = result.bounds.cell_size_m
    for ix, iy in ((0, 0), (3, 7), (12, 15), (20, 19)):
        south, west, north, east = cell_corners(ix, iy, result)
        x_sw, y_sw = project_m(south, west, result.bounds)
        x_ne, y_ne = project_m(north, east, result.bounds)
        assert x_sw == pytest.approx(ix * size, abs=1e-6)
        assert y_sw == pytest.approx(iy * size, abs=1e-6)
        assert x_ne == pytest.approx((ix + 1) * size, abs=1e-6)
        assert y_ne == pytest.approx((iy + 1) * size, abs=1e-6)


def test_cell_corners_are_centred_on_the_cell_center(result):
    """Середина прямоугольника совпадает с центром клетки из сетки."""
    for ix, iy in ((0, 0), (6, 13), (20, 19)):
        south, west, north, east = cell_corners(ix, iy, result)
        center_lat, center_lon = cell_center(ix, iy, result.bounds)
        assert (south + north) / 2.0 == pytest.approx(center_lat, abs=1e-12)
        assert (west + east) / 2.0 == pytest.approx(center_lon, abs=1e-12)
        assert south < center_lat < north
        assert west < center_lon < east


def test_cell_step_equals_one_cell_of_the_grid(result):
    """Шаг сетки в градусах соответствует размеру клетки в метрах."""
    step_lat, step_lon = cell_step(result.bounds)
    assert step_lat > 0.0 and step_lon > 0.0
    x_step, _ = project_m(result.bounds.grid_origin_lat,
                          result.bounds.grid_origin_lon + step_lon, result.bounds)
    _, y_step = project_m(result.bounds.grid_origin_lat + step_lat,
                          result.bounds.grid_origin_lon, result.bounds)
    assert x_step == pytest.approx(result.bounds.cell_size_m, abs=1e-6)
    assert y_step == pytest.approx(result.bounds.cell_size_m, abs=1e-6)


def test_payload_rectangles_hold_their_own_centers(result, payload):
    """Каждый прямоугольник содержит центр своей клетки, а не соседней."""
    centers = {cell.cell_id: (cell.center_lat, cell.center_lon) for cell in result.cells}
    for item in payload["cells"]:
        lat, lon = centers[item["id"]]
        assert item["s"] < lat < item["n"]
        assert item["w"] < lon < item["e"]


def test_heat_covers_every_accounted_cell(result, payload):
    """Тепловые данные есть и описывают все учтённые клетки."""
    heat = payload["heat"]
    assert set(heat) == {"median_min", "median_max", "step_lat", "step_lon", "points"}
    assert len(heat["points"]) == len(result.cells)
    centers = {(cell.center_lat, cell.center_lon) for cell in result.cells}
    assert {(p["lat"], p["lon"]) for p in heat["points"]} == centers


def test_heat_is_normalized_over_the_actual_median_range(result, payload):
    """Границы нормировки — фактические минимум и максимум медиан в данных."""
    heat = payload["heat"]
    medians = [cell.rsrp_median for cell in result.cells]
    assert heat["median_min"] == min(medians)
    assert heat["median_max"] == max(medians)

    values = [point["t"] for point in heat["points"]]
    assert all(0.0 <= value <= 1.0 for value in values)
    assert min(values) == 0.0
    assert max(values) == 1.0


def test_heat_normalization_does_not_use_spec_thresholds(result):
    """Нормировка по данным, а не по порогам -105/-90.

    Берём только зелёные клетки: по константам спеки такая выборка дала бы
    узкую холодную полосу, а нормировка по данным обязана растянуть её на
    весь диапазон [0, 1] и остаться читаемой.
    """
    green = [cell for cell in result.cells if cell.rsrp_median >= RSRP_GREEN_MIN]
    distinct = {cell.rsrp_median for cell in green}
    if len(distinct) < 2:
        pytest.skip("в наборе меньше двух разных медиан в зелёной зоне")

    heat = build_heat(replace(result, cells=green))
    assert heat["median_min"] == min(distinct)
    assert heat["median_max"] == max(distinct)
    assert heat["median_min"] >= RSRP_GREEN_MIN > RSRP_YELLOW_MIN
    values = [point["t"] for point in heat["points"]]
    assert min(values) == 0.0
    assert max(values) == 1.0


def test_heat_is_hotter_where_the_signal_is_weaker(result, payload):
    """Чем ниже медиана RSRP, тем выше интенсивность."""
    # Ключ — пара координат: у клеток одного ряда широта центра совпадает.
    by_center = {(point["lat"], point["lon"]): point["t"]
                 for point in payload["heat"]["points"]}
    assert len(by_center) == len(result.cells)

    ranked = sorted(result.cells, key=lambda cell: (cell.rsrp_median, cell.cell_id))
    weakest, strongest = ranked[0], ranked[-1]
    assert by_center[(weakest.center_lat, weakest.center_lon)] == 1.0
    assert by_center[(strongest.center_lat, strongest.center_lon)] == 0.0

    pairs = sorted((cell.rsrp_median, by_center[(cell.center_lat, cell.center_lon)])
                   for cell in result.cells)
    intensities = [value for _, value in pairs]
    assert intensities == sorted(intensities, reverse=True)


def test_heat_intensity_endpoints_and_middle():
    """Крайние медианы дают 0 и 1, середина диапазона — 0.5."""
    assert heat_intensity(-120.0, -120.0, -80.0) == 1.0
    assert heat_intensity(-80.0, -120.0, -80.0) == 0.0
    assert heat_intensity(-100.0, -120.0, -80.0) == pytest.approx(0.5)


def test_heat_intensity_survives_a_degenerate_range():
    """Все медианы равны — делить не на что, слой ровный, без деления на ноль."""
    assert heat_intensity(-95.0, -95.0, -95.0) == 0.5


def test_heat_step_lets_the_page_scale_the_radius(result, payload):
    """step_lat/step_lon в payload — это шаг сетки, он нужен странице."""
    step_lat, step_lon = cell_step(result.bounds)
    assert payload["heat"]["step_lat"] == step_lat
    assert payload["heat"]["step_lon"] == step_lon


def test_build_heat_on_a_single_cell(result):
    """Одна клетка — диапазона нет, интенсивность середина, деления на ноль нет."""
    heat = build_heat(replace(result, cells=result.cells[:1]))
    assert len(heat["points"]) == 1
    assert heat["points"][0]["t"] == 0.5
    assert heat["median_min"] == heat["median_max"] == result.cells[0].rsrp_median


def test_page_survives_an_empty_grid(result, tmp_path):
    """Клеток нет — страница всё равно собирается, тепловых точек ноль."""
    empty = replace(result, cells=[])
    heat = build_heat(empty)
    assert heat["points"] == []
    assert heat["median_min"] == heat["median_max"] == 0.0
    text = render_map(empty, tmp_path / "map.html").read_text(encoding="utf-8")
    assert PLACEHOLDER.findall(text) == []
    # Страница сама не включает пустой слой в переключатель.
    assert "DATA.heat.points.length" in text


def test_heat_layer_is_an_overlay_and_off_by_default(html):
    """Тепловой слой — оверлей второго аргумента control.layers, не подложка."""
    control = re.search(r"L\.control\.layers\(\s*([\w]+),\s*([\w]+),", html)
    assert control is not None
    assert control.group(1) == "basemaps"
    assert control.group(2) == "overlays"
    assert re.search(r'overlays\["' + HEAT_LAYER_NAME + r'"\]\s*=\s*new HeatLayer',
                     html) is not None
    # Слой не добавляется на карту при загрузке: по умолчанию выключен.
    assert re.search(r"new HeatLayer\([^)]*\)\s*\.addTo", html) is None
    assert f'basemaps["{HEAT_LAYER_NAME}"]' not in html


def test_layers_control_is_not_hidden_under_the_panel(html):
    """Переключатель слоёв обязан быть доступен: иначе тепловой слой не включить.

    Панель сводки стоит в правом верхнем углу и имеет тот же z-index 1000,
    что и контейнер контролов Leaflet, но идёт в разметке позже — то есть
    накрывает всё, что Leaflet положил бы в тот же угол. Поэтому переключатель
    переносится в другой угол явным position.
    """
    control = re.search(r"L\.control\.layers\([^;]*\);", html, re.DOTALL)
    assert control is not None
    assert re.search(r'position:\s*"(topleft|bottomleft|bottomright)"',
                     control.group(0)) is not None
    assert "top: 12px; right: 12px" in html   # панель осталась на своём месте


def test_page_loads_no_external_script_or_stylesheet(html):
    """Страница самодостаточна: внешних скриптов и стилей нет вообще.

    Leaflet встроен из vendor/, тепловой слой нарисован своим кодом.
    Единственные внешние запросы, которые делает страница, — плитки
    подложки, и без них карта остаётся полностью читаемой.
    """
    assert re.findall(r'<script[^>]*\ssrc="([^"]+)"', html) == []
    assert re.findall(r'<link[^>]*\shref="([^"]+\.css)"', html) == []
    assert "unpkg.com" not in html
    assert "leaflet-heat" not in html
    assert "simpleheat" not in html


def test_leaflet_is_inlined_with_its_icons(html):
    """Leaflet встроен целиком, а его картинки — как data: URI."""
    assert "Leaflet 1.9.4" in html
    assert "url(images/" not in html          # относительные пути сломались бы
    assert "url(data:image/png;base64," in html


def test_heat_legend_explains_the_layer(html, payload):
    """В панели есть пояснение к тепловому слою и границы нормировки."""
    assert "Тепловая карта сигнала" in html
    assert "heatbar" in html
    for _, color in HEAT_GRADIENT:
        assert color in html
    assert f"{payload['heat']['median_min']:.1f}" in html
    assert f"{payload['heat']['median_max']:.1f}" in html


def test_existing_map_layers_are_intact(html):
    """Регресс-защита: тепловой слой не должен сломать уже сделанную карту."""
    for name in ("Светло-серая", "Улицы", "OpenStreetMap", "Без подложки"):
        assert f'"{name}"' in html
    assert 'basemaps["Светло-серая"].addTo(map)' in html
    for call in ("L.rectangle(", "L.marker(", "L.circleMarker(", "L.circle("):
        assert call in html


def test_summary_panel_repeats_the_result(result, html):
    """Сводка в панели повторяет числа прогона."""
    assert f"Слабых клеток: <b>{len(result.weak)}</b>" in html
    assert f"Отброшено строк: <b>{result.report.dropped_rows}</b>" in html
    assert f"Покрыто слабых: <b>{result.covered_weak_cells}</b>" in html
    for site_id in result.best_candidates:
        assert site_id in html


def test_towers_and_chosen_sites_are_in_the_payload(result, payload):
    """Существующие БС и выбранные площадки по-прежнему попадают на карту."""
    assert len(payload["towers"]) == len(result.towers)
    assert [s["id"] for s in payload["chosen"]] == result.best_candidates
    assert [c["id"] for c in payload["worst"]] == [c.cell_id for c in result.worst]
