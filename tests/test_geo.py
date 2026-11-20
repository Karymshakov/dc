"""Проверки геометрии: гаверсинус и локальная сетка."""
from __future__ import annotations

import math

import pytest

from rsrp_coverage.config import Bounds
from rsrp_coverage.geo import cell_center, cell_id, cell_index, haversine_m, project_m

R = 6371000.0


@pytest.fixture
def bounds() -> Bounds:
    return Bounds(
        lat_min=42.84, lat_max=42.87597286423675,
        lon_min=74.56, lon_max=74.60905912302184,
        grid_origin_lat=42.84, grid_origin_lon=74.56,
        cell_size_m=200.0, earth_radius_m=R, timezone="Asia/Bishkek",
    )


def test_haversine_zero_distance():
    assert haversine_m(42.85, 74.58, 42.85, 74.58, R) == pytest.approx(0.0)


def test_haversine_one_degree_of_latitude():
    """Градус широты — это R * pi / 180."""
    expected = R * math.pi / 180.0
    assert haversine_m(0.0, 0.0, 1.0, 0.0, R) == pytest.approx(expected, rel=1e-9)


def test_haversine_is_symmetric():
    forward = haversine_m(42.84, 74.56, 42.86, 74.60, R)
    backward = haversine_m(42.86, 74.60, 42.84, 74.56, R)
    assert forward == pytest.approx(backward)


def test_origin_projects_to_zero(bounds):
    x, y = project_m(bounds.grid_origin_lat, bounds.grid_origin_lon, bounds)
    assert x == pytest.approx(0.0)
    assert y == pytest.approx(0.0)


def test_origin_is_cell_zero_zero(bounds):
    assert cell_index(bounds.grid_origin_lat, bounds.grid_origin_lon, bounds) == (0, 0)


def test_cell_id_is_zero_padded():
    assert cell_id(6, 13) == "X006_Y013"
    assert cell_id(0, 0) == "X000_Y000"


def test_cell_center_round_trips_to_same_cell(bounds):
    """Центр клетки обязан попадать обратно в ту же клетку."""
    for ix, iy in ((0, 0), (6, 13), (5, 12), (20, 19)):
        lat, lon = cell_center(ix, iy, bounds)
        assert cell_index(lat, lon, bounds) == (ix, iy)


def test_cell_size_is_200_m(bounds):
    """Соседние центры отстоят ровно на размер клетки."""
    lat_a, lon_a = cell_center(5, 5, bounds)
    lat_b, lon_b = cell_center(6, 5, bounds)
    assert haversine_m(lat_a, lon_a, lat_b, lon_b, R) == pytest.approx(200.0, rel=1e-3)

    lat_c, lon_c = cell_center(5, 6, bounds)
    assert haversine_m(lat_a, lon_a, lat_c, lon_c, R) == pytest.approx(200.0, rel=1e-3)


def test_floor_rule_on_negative_side(bounds):
    """Точка южнее начала сетки даёт отрицательный индекс, а не нулевой."""
    ix, iy = cell_index(bounds.grid_origin_lat - 0.001, bounds.grid_origin_lon, bounds)
    assert iy < 0


def test_x_uses_cosine_of_grid_origin_not_of_the_point(bounds):
    """cos берётся от широты НАЧАЛА СЕТКИ, а не от широты точки.

    Если взять cos(lat точки), сетка «поедет» с ростом широты и клетки
    разойдутся с эталоном. Проверяем так: одинаковый сдвиг по долготе на
    двух разных широтах обязан дать одинаковый x.
    """
    delta_lon = 0.01
    x_low, _ = project_m(bounds.lat_min, bounds.grid_origin_lon + delta_lon, bounds)
    x_high, _ = project_m(bounds.lat_max, bounds.grid_origin_lon + delta_lon, bounds)
    assert x_low == pytest.approx(x_high, rel=1e-12)


def test_x_matches_the_specified_formula(bounds):
    """Дословная формула спеки: x = R * cos(rad(lat0)) * rad(lon - lon0)."""
    lon = bounds.grid_origin_lon + 0.02
    expected = (R * math.cos(math.radians(bounds.grid_origin_lat))
                * math.radians(lon - bounds.grid_origin_lon))
    x, _ = project_m(bounds.lat_max, lon, bounds)
    assert x == pytest.approx(expected, rel=1e-12)
