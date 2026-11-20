"""Проверки правил, которые соединяются только в pipeline.

На открытом наборе организаторов эти правила нельзя проверить: там худшие
клетки и так красные, а недопустимые площадки и так проигрывают. Поэтому
здесь строится маленький синтетический район, где нарушение правила меняет
ответ. Если убрать отсечку 500 м или искать худших только среди красных —
эти тесты падают.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsrp_coverage.config import Bounds
from rsrp_coverage.geo import cell_center
from rsrp_coverage.pipeline import run

BOUNDS = {
    "lat_min": 0.0, "lat_max": 0.06,
    "lon_min": 0.0, "lon_max": 0.06,
    "grid_origin_lat": 0.0, "grid_origin_lon": 0.0,
    "cell_size_m": 200, "earth_radius_m": 6371000, "timezone": "UTC",
}
STAMP = "2026-11-16T13:58:{:02d}+06:00"


def build_dataset(tmp_path: Path, cells: dict[tuple[int, int], float],
                  towers: list[tuple[str, tuple[int, int]]],
                  candidates: list[tuple[str, tuple[int, int]]]) -> Path:
    """Создать каталог данных: клетки по медиане, вышки и кандидаты по клеткам."""
    directory = tmp_path / "synthetic"
    directory.mkdir()
    (directory / "bounds.json").write_text(json.dumps(BOUNDS), encoding="utf-8")
    bounds = Bounds.from_json(directory / "bounds.json")

    rows = ["lat,lon,rsrp_dbm,timestamp"]
    second = 0
    for (ix, iy), median in cells.items():
        lat, lon = cell_center(ix, iy, bounds)
        for _ in range(5):  # ровно минимум, чтобы клетка учитывалась
            rows.append(f"{lat:.8f},{lon:.8f},{median},{STAMP.format(second % 60)}")
            second += 1
    (directory / "measurements.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    for name, sites in (("towers.csv", towers), ("candidates.csv", candidates)):
        lines = ["id,lat,lon"]
        for site_id, (ix, iy) in sites:
            lat, lon = cell_center(ix, iy, bounds)
            lines.append(f"{site_id},{lat:.8f},{lon:.8f}")
        (directory / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    return directory


def test_worst_cells_include_non_red_cells(tmp_path):
    """Худшие ищутся среди ВСЕХ клеток, даже если красных меньше трёх.

    Здесь красных всего две, а третья худшая — жёлтая. Если искать худших
    только среди красных, в ответе окажется два элемента вместо трёх.
    """
    cells = {
        (1, 1): -130.0,   # красная
        (2, 1): -110.0,   # красная
        (3, 1): -100.0,   # жёлтая, но третья по слабости
        (4, 1): -80.0,    # зелёная
    }
    directory = build_dataset(
        tmp_path, cells,
        towers=[("B01", (9, 9))],
        candidates=[("C01", (1, 2)), ("C02", (3, 2))],
    )
    result = run(directory)

    assert len(result.weak) == 2
    assert [cell.cell_id for cell in result.worst] == ["X001_Y001", "X002_Y001", "X003_Y001"]
    assert result.worst[2].rsrp_median == pytest.approx(-100.0)


def test_candidate_on_top_of_a_tower_cannot_win(tmp_path):
    """Отсечка 500 м реально применяется при выборе пары.

    C01 стоит на координатах вышки и единственный достаёт до красной клетки
    (1,1). Если отсечку не применить, перебор выберет C01 и насчитает две
    покрытые клетки вместо одной.
    """
    cells = {
        (1, 1): -130.0,     # красная, рядом только с недопустимым C01
        (20, 20): -120.0,   # красная, достаёт только C02
        (5, 1): -80.0,      # зелёная, чтобы район не состоял из одних красных
    }
    directory = build_dataset(
        tmp_path, cells,
        towers=[("B01", (1, 1))],
        candidates=[
            ("C01", (1, 1)),    # на вышке -> недопустим, покрыл бы (1,1)
            ("C02", (20, 20)),  # допустим, покрывает (20,20)
            ("C03", (25, 25)),  # допустим, но до красных больше 1 км
        ],
    )
    result = run(directory)

    eligible_ids = {site.id for site in result.eligible}
    assert "C01" not in eligible_ids
    assert eligible_ids == {"C02", "C03"}
    # Без отсечки победила бы пара C01+C02 с охватом 2.
    assert result.best_candidates == ["C02", "C03"]
    assert result.covered_weak_cells == 1


def test_fewer_than_two_eligible_sites_does_not_crash(tmp_path):
    """Вырожденный вход не должен ронять прогон."""
    cells = {(1, 1): -130.0}
    directory = build_dataset(
        tmp_path, cells,
        towers=[("B01", (1, 1)), ("B02", (2, 1))],
        candidates=[("C01", (1, 1)), ("C02", (2, 1))],
    )
    result = run(directory)

    assert result.eligible == []
    assert result.best_candidates == []
    assert result.covered_weak_cells == 0


def test_no_weak_cells_yields_empty_coverage(tmp_path):
    """Красных клеток нет — пара выбирается, но покрывать нечего."""
    cells = {(1, 1): -80.0, (2, 1): -85.0}
    directory = build_dataset(
        tmp_path, cells,
        towers=[("B01", (9, 9))],
        candidates=[("C01", (1, 1)), ("C02", (2, 1))],
    )
    result = run(directory)

    assert result.weak == []
    assert result.covered_weak_cells == 0
    assert len(result.best_candidates) == 2
