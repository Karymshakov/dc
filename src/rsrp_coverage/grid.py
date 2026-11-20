"""Агрегация замеров в клетки сетки 200 м."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .cleaning import Measurement
from .config import (MIN_MEASUREMENTS_PER_CELL, RSRP_GREEN_MIN, RSRP_YELLOW_MIN,
                     WORST_CELLS_COUNT, Bounds)
from .geo import cell_center, cell_id, cell_index


@dataclass(frozen=True)
class Cell:
    cell_id: str
    ix: int
    iy: int
    rsrp_median: float
    sample_count: int
    center_lat: float
    center_lon: float

    @property
    def color(self) -> str:
        """Зелёный >= -90; жёлтый [-105, -90); красный < -105."""
        if self.rsrp_median >= RSRP_GREEN_MIN:
            return "green"
        if self.rsrp_median >= RSRP_YELLOW_MIN:
            return "yellow"
        return "red"

    @property
    def is_weak(self) -> bool:
        """Слабая (красная) зона."""
        return self.color == "red"


def build_grid(measurements: list[Measurement], bounds: Bounds) -> list[Cell]:
    """Сгруппировать замеры по клеткам и посчитать медиану RSRP.

    Клетки, в которых меньше MIN_MEASUREMENTS_PER_CELL замеров, не учитываются.
    """
    buckets: dict[tuple[int, int], list[float]] = {}
    for item in measurements:
        key = cell_index(item.lat, item.lon, bounds)
        buckets.setdefault(key, []).append(item.rsrp_dbm)

    cells: list[Cell] = []
    for (ix, iy), values in buckets.items():
        if len(values) < MIN_MEASUREMENTS_PER_CELL:
            continue
        lat, lon = cell_center(ix, iy, bounds)
        cells.append(Cell(
            cell_id=cell_id(ix, iy),
            ix=ix,
            iy=iy,
            rsrp_median=float(median(values)),
            sample_count=len(values),
            center_lat=lat,
            center_lon=lon,
        ))
    cells.sort(key=lambda cell: cell.cell_id)
    return cells


def count_cells_with_measurements(measurements: list[Measurement], bounds: Bounds) -> int:
    """Сколько клеток вообще содержат замеры, до отсечки по минимуму."""
    return len({cell_index(m.lat, m.lon, bounds) for m in measurements})


def worst_cells(cells: list[Cell], limit: int = WORST_CELLS_COUNT) -> list[Cell]:
    """Худшие клетки: сортировка по медиане, затем по cell_id."""
    return sorted(cells, key=lambda cell: (cell.rsrp_median, cell.cell_id))[:limit]


def weak_cells(cells: list[Cell]) -> list[Cell]:
    """Красные клетки, медиана ниже -105."""
    return [cell for cell in cells if cell.is_weak]
