"""Проверки СМЫСЛА бонусов, а не только их наличия.

Адвокат дьявола показал, что бонусы можно вывернуть наизнанку при зелёном
прогоне: инвертировать палитру тепловой карты, развернуть полоску легенды,
переставить подписи min/max, подменить определение медианного сдвига. Все
эти мутации меняли то единственное, что видит жюри, — отрисованную страницу
и числа в отчёте, — но ни один тест их не замечал.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rsrp_coverage.cleaning import Measurement
from rsrp_coverage.config import Bounds
from rsrp_coverage.daynight import compare_day_night
from rsrp_coverage.geo import cell_center
from rsrp_coverage.mapping import (HEAT_GRADIENT, heat_css_gradient,
                                   heat_intensity, heat_js_stops, render_map)
from rsrp_coverage.pipeline import run

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "07_coverage"
BISHKEK = timezone(timedelta(hours=6))


# --- Тепловая карта: направление палитры -----------------------------------

def test_hot_end_of_the_palette_is_the_weak_signal():
    """Стоп 1.0 — «горячий» конец, и он соответствует слабому сигналу.

    heat_intensity даёт 1.0 самой низкой медиане. Значит цвет на стопе 1.0
    обязан быть тёплым, а на стопе 0.0 — холодным. Инверсия палитры
    переворачивает смысл всего слоя.
    """
    assert heat_intensity(-130.0, -130.0, -70.0) == pytest.approx(1.0)
    assert heat_intensity(-70.0, -130.0, -70.0) == pytest.approx(0.0)

    cold = HEAT_GRADIENT[0]
    hot = HEAT_GRADIENT[-1]
    assert cold[0] == 0.0 and hot[0] == 1.0

    def red_minus_blue(colour: str) -> int:
        return int(colour[1:3], 16) - int(colour[5:7], 16)

    assert red_minus_blue(hot[1]) > 0, "конец 1.0 обязан быть тёплым"
    assert red_minus_blue(cold[1]) < 0, "конец 0.0 обязан быть холодным"


def test_css_bar_and_canvas_palette_are_the_same_sequence():
    """Полоска легенды и градиент внутри canvas строятся из одного источника.

    Если они разойдутся, легенда начнёт врать про то, что нарисовано.
    """
    stops = json.loads(heat_js_stops())
    css = heat_css_gradient()

    assert [stop for stop, _ in stops] == [stop for stop, _ in HEAT_GRADIENT]
    assert [colour for _, colour in stops] == [colour for _, colour in HEAT_GRADIENT]

    from_css = re.findall(r"(#[0-9a-fA-F]{6})\s+([\d.]+)%", css)
    assert [colour for colour, _ in from_css] == [colour for _, colour in HEAT_GRADIENT]
    assert [float(pos) / 100 for _, pos in from_css] == [
        pytest.approx(stop) for stop, _ in HEAT_GRADIENT]


def test_legend_labels_sit_on_the_right_ends(tmp_path):
    """В легенде «сильный» стоит у максимума медиан, «слабый» — у минимума."""
    result = run(DATA_DIR)
    html = render_map(result, tmp_path / "map.html").read_text(encoding="utf-8")
    medians = [cell.rsrp_median for cell in result.cells]

    strong = re.search(r"сильный\s+(-?[\d.]+)", html)
    weak = re.search(r"(-?[\d.]+)\s+слабый", html)
    assert strong and weak

    assert float(strong.group(1)) == pytest.approx(max(medians), abs=0.05)
    assert float(weak.group(1)) == pytest.approx(min(medians), abs=0.05)
    assert float(strong.group(1)) > float(weak.group(1))


def test_css_bar_runs_from_cold_to_hot(tmp_path):
    """Полоска легенды идёт слева направо тем же порядком, что и палитра."""
    html = render_map(run(DATA_DIR), tmp_path / "map.html").read_text(encoding="utf-8")
    bar = re.search(r"linear-gradient\((\d+)deg,\s*([^)]+)\)", html)
    assert bar, "в легенде нет полоски градиента"
    assert bar.group(1) == "90", "разворот полоски переворачивает смысл легенды"
    colours = re.findall(r"#[0-9a-fA-F]{6}", bar.group(2))
    assert colours == [colour for _, colour in HEAT_GRADIENT]


# --- День и ночь: определения чисел в отчёте -------------------------------

def make_bounds() -> Bounds:
    return Bounds(
        lat_min=0.0, lat_max=0.06, lon_min=0.0, lon_max=0.06,
        grid_origin_lat=0.0, grid_origin_lon=0.0,
        cell_size_m=200.0, earth_radius_m=6371000.0, timezone="Asia/Bishkek",
    )


def cell_measurements(bounds: Bounds, ix: int, iy: int, hour: int,
                      values: list[float]) -> list[Measurement]:
    lat, lon = cell_center(ix, iy, bounds)
    stamp = datetime(2026, 11, 16, hour, 30, 0, tzinfo=BISHKEK)
    return [Measurement(lat=lat, lon=lon, rsrp_dbm=value, timestamp=stamp)
            for value in values]


def test_abs_shift_is_median_of_absolutes_not_absolute_of_median():
    """median(|сдвиг|) и |median(сдвиг)| — разные числа, и в отчёте первое.

    Вход подобран так, что определения расходятся: сдвиги +4 и -4 дают
    median(сдвиг) = 0, а median(|сдвиг|) = 4. Подмена одного другим меняет
    число, которое мы цитируем в README, и не ломает ничего другого.
    """
    bounds = make_bounds()
    day = (cell_measurements(bounds, 1, 1, 12, [-100.0] * 5)
           + cell_measurements(bounds, 3, 1, 12, [-100.0] * 5))
    night = (cell_measurements(bounds, 1, 1, 2, [-104.0] * 5)
             + cell_measurements(bounds, 3, 1, 2, [-96.0] * 5))

    comparison = compare_day_night(day + night, bounds)

    assert comparison.median_shift_db == pytest.approx(0.0)
    assert comparison.median_abs_shift_db == pytest.approx(4.0)


def test_shift_sign_is_night_minus_day():
    """Знак сдвига: ночь минус день. Ночью слабее — сдвиг отрицательный."""
    bounds = make_bounds()
    day = cell_measurements(bounds, 1, 1, 12, [-100.0] * 5)
    night = cell_measurements(bounds, 1, 1, 2, [-110.0] * 5)

    comparison = compare_day_night(day + night, bounds)
    assert comparison.median_shift_db == pytest.approx(-10.0)


def test_report_keeps_two_decimal_places():
    """Отчёт округляется до сотых: это задокументировано, значит проверяется."""
    bounds = make_bounds()
    day = cell_measurements(bounds, 1, 1, 12, [-100.0, -100.0, -100.0, -101.0, -101.0])
    night = cell_measurements(bounds, 1, 1, 2, [-103.5] * 5)

    payload = compare_day_night(day + night, bounds).to_json()
    shift = payload["comparison"]["median_shift_night_minus_day_db"]
    assert shift == pytest.approx(-3.5, abs=1e-9)
    assert shift == round(shift, 2)

    for half in ("day", "night"):
        median = payload[half]["median_of_medians_dbm"]
        if median is not None:
            assert median == round(median, 2)
