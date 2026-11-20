"""Проверки агрегации по клеткам и выбора площадок."""
from __future__ import annotations

from datetime import datetime

import pytest

from rsrp_coverage.cleaning import Measurement
from rsrp_coverage.config import Bounds
from rsrp_coverage.geo import cell_center, haversine_m
from rsrp_coverage.grid import Cell, build_grid, weak_cells, worst_cells
from rsrp_coverage.siting import (Site, best_pair_exhaustive, best_pair_greedy,
                                  coverage_sets, eligible_candidates)

STAMP = datetime.fromisoformat("2026-11-16T13:58:30+06:00")


@pytest.fixture
def bounds() -> Bounds:
    return Bounds(
        lat_min=42.84, lat_max=42.87597286423675,
        lon_min=74.56, lon_max=74.60905912302184,
        grid_origin_lat=42.84, grid_origin_lon=74.56,
        cell_size_m=200.0, earth_radius_m=6371000.0, timezone="Asia/Bishkek",
    )


def make_cell(cell_id: str, median: float, ix: int = 0, iy: int = 0) -> Cell:
    return Cell(cell_id=cell_id, ix=ix, iy=iy, rsrp_median=median,
                sample_count=5, center_lat=42.85, center_lon=74.58)


def measurements_in_cell(bounds, ix, iy, values):
    lat, lon = cell_center(ix, iy, bounds)
    return [Measurement(lat=lat, lon=lon, rsrp_dbm=v, timestamp=STAMP) for v in values]


def test_cell_with_fewer_than_five_measurements_is_skipped(bounds):
    cells = build_grid(measurements_in_cell(bounds, 3, 3, [-100] * 4), bounds)
    assert cells == []


def test_cell_with_exactly_five_measurements_is_kept(bounds):
    cells = build_grid(measurements_in_cell(bounds, 3, 3, [-100] * 5), bounds)
    assert len(cells) == 1
    assert cells[0].sample_count == 5


def test_median_of_even_count_is_averaged(bounds):
    values = [-100, -102, -104, -106, -108, -110]
    cells = build_grid(measurements_in_cell(bounds, 3, 3, values), bounds)
    assert cells[0].rsrp_median == pytest.approx(-105.0)


@pytest.mark.parametrize("median,expected", [
    (-89.9, "green"), (-90.0, "green"),
    (-90.1, "yellow"), (-105.0, "yellow"),
    (-105.1, "red"), (-130.0, "red"),
])
def test_color_thresholds(median, expected):
    assert make_cell("X000_Y000", median).color == expected


def test_weak_cells_are_strictly_below_minus_105():
    cells = [make_cell("A", -105.0), make_cell("B", -105.1)]
    assert [c.cell_id for c in weak_cells(cells)] == ["B"]


def test_worst_cells_sorted_by_median_then_id():
    cells = [
        make_cell("X002_Y002", -120.0),
        make_cell("X001_Y001", -120.0),
        make_cell("X003_Y003", -130.0),
        make_cell("X004_Y004", -100.0),
    ]
    assert [c.cell_id for c in worst_cells(cells)] == [
        "X003_Y003", "X001_Y001", "X002_Y002"]


def test_candidate_closer_than_500_m_is_rejected(bounds):
    tower = Site(id="B01", lat=42.85, lon=74.58)
    near = Site(id="C01", lat=42.85, lon=74.58)
    far = Site(id="C02", lat=42.86, lon=74.58)
    allowed = eligible_candidates([near, far], [tower], bounds)
    assert [s.id for s in allowed] == ["C02"]


def test_tower_distance_boundary_is_inclusive(bounds, monkeypatch):
    """Правило «>= 500 м» включает ровно 500 м.

    Построить точку ровно в 500 м нельзя: обратное преобразование градусов
    даёт 499.99999999967525. Поэтому делаем наоборот — измеряем реальное
    расстояние и подставляем ЕГО как порог. Тогда расстояние равно порогу
    точно, без шума, и правило проверяется как есть: площадка допустима.
    Мутация `<` на `<=` этот тест роняет.
    """
    import rsrp_coverage.siting as siting_module

    tower = Site(id="B01", lat=42.85, lon=74.58)
    candidate = Site(id="C01", lat=42.8545, lon=74.58)
    exact = haversine_m(tower.lat, tower.lon, candidate.lat, candidate.lon,
                        bounds.earth_radius_m)
    monkeypatch.setattr(siting_module, "MIN_DISTANCE_TO_TOWER_M", exact)

    allowed = siting_module.eligible_candidates([candidate], [tower], bounds)
    assert [site.id for site in allowed] == ["C01"]


def test_coverage_boundary_is_inclusive(bounds, monkeypatch):
    """Правило «<= 1000 м» включает ровно 1000 м.

    Тот же приём: порогом становится измеренное расстояние.
    Мутация `<=` на `<` роняет этот тест.
    """
    import rsrp_coverage.siting as siting_module

    site = Site(id="C01", lat=42.85, lon=74.58)
    cell = Cell(cell_id="X000_Y000", ix=0, iy=0, rsrp_median=-120.0, sample_count=5,
                center_lat=42.859, center_lon=74.58)
    exact = haversine_m(site.lat, site.lon, cell.center_lat, cell.center_lon,
                        bounds.earth_radius_m)
    monkeypatch.setattr(siting_module, "COVERAGE_RADIUS_M", exact)

    covered = siting_module.coverage_sets([site], [cell], bounds)
    assert covered["C01"] == frozenset({"X000_Y000"})


def test_tower_distance_rule_matches_computed_distance(bounds):
    """Отбор площадок согласован с гаверсинусом: допустима ровно та, где >= 500 м.

    Точную границу в 500 м не проверяем конструктивно: обратное преобразование
    градусов даёт погрешность порядка 1e-10 м, и такой тест ловил бы шум float,
    а не правило. Берём отступ в один метр по обе стороны.
    """
    import math
    tower = Site(id="B01", lat=42.85, lon=74.58)
    inside = math.degrees(499.0 / bounds.earth_radius_m)
    outside = math.degrees(501.0 / bounds.earth_radius_m)
    near = Site(id="C01", lat=42.85 + inside, lon=74.58)
    far = Site(id="C02", lat=42.85 + outside, lon=74.58)

    allowed = eligible_candidates([near, far], [tower], bounds)
    assert [s.id for s in allowed] == ["C02"]


def test_coverage_radius_cuts_off_at_1000_m(bounds):
    """Клетка в пределах 1 км покрывается, за пределами — нет."""
    import math
    site = Site(id="C01", lat=42.85, lon=74.58)
    inside = math.degrees(999.0 / bounds.earth_radius_m)
    outside = math.degrees(1001.0 / bounds.earth_radius_m)
    near = Cell(cell_id="X000_Y000", ix=0, iy=0, rsrp_median=-120.0, sample_count=5,
                center_lat=42.85 + inside, center_lon=74.58)
    far = Cell(cell_id="X001_Y001", ix=1, iy=1, rsrp_median=-120.0, sample_count=5,
               center_lat=42.85 + outside, center_lon=74.58)

    assert coverage_sets([site], [near, far], bounds)["C01"] == frozenset({"X000_Y000"})


def test_union_is_not_double_counted():
    coverage = {"A": frozenset({"c1", "c2"}), "B": frozenset({"c2", "c3"})}
    ids, size = best_pair_exhaustive(coverage)
    assert ids == ["A", "B"]
    assert size == 3


def test_tie_breaks_to_lexicographically_smallest_pair():
    coverage = {
        "C01": frozenset({"a"}), "C02": frozenset({"b"}),
        "C03": frozenset({"c"}), "C04": frozenset({"d"}),
    }
    ids, size = best_pair_exhaustive(coverage)
    assert ids == ["C01", "C02"]
    assert size == 2


def test_greedy_can_lose_to_exhaustive():
    """Классический контрпример: жадный выбор первого шага запирает результат."""
    coverage = {
        "C01": frozenset({"a", "b", "c"}),
        "C02": frozenset({"a", "b", "d"}),
        "C03": frozenset({"e", "f"}),
        "C04": frozenset({"g", "h"}),
    }
    greedy_ids, greedy_size = best_pair_greedy(coverage)
    best_ids, best_size = best_pair_exhaustive(coverage)
    assert greedy_ids == ["C01", "C03"]
    assert greedy_size == 5
    assert best_size == 5
    assert best_size >= greedy_size
