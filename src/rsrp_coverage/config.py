"""Константы задания. Любое изменение здесь ломает соответствие эталону."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Границы валидного RSRP, дБм. Обе границы включительно.
RSRP_MIN_DBM = -140.0
RSRP_MAX_DBM = -44.0

# Пороги цветов карты.
RSRP_GREEN_MIN = -90.0      # >= -90 зелёный
RSRP_YELLOW_MIN = -105.0    # [-105, -90) жёлтый; < -105 красный (слабая зона)

MIN_MEASUREMENTS_PER_CELL = 5
WORST_CELLS_COUNT = 3

# Правила размещения новой БС, метры.
MIN_DISTANCE_TO_TOWER_M = 500.0
COVERAGE_RADIUS_M = 1000.0
SITES_TO_PICK = 2


@dataclass(frozen=True)
class Bounds:
    """Прямоугольник района и параметры сетки из bounds.json. Границы включительно."""

    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    grid_origin_lat: float
    grid_origin_lon: float
    cell_size_m: float
    earth_radius_m: float
    timezone: str

    @classmethod
    def from_json(cls, path: str | Path) -> "Bounds":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            lat_min=float(data["lat_min"]),
            lat_max=float(data["lat_max"]),
            lon_min=float(data["lon_min"]),
            lon_max=float(data["lon_max"]),
            grid_origin_lat=float(data["grid_origin_lat"]),
            grid_origin_lon=float(data["grid_origin_lon"]),
            cell_size_m=float(data["cell_size_m"]),
            earth_radius_m=float(data["earth_radius_m"]),
            timezone=str(data.get("timezone", "Asia/Bishkek")),
        )

    def contains(self, lat: float, lon: float) -> bool:
        """Точка внутри района. Границы включены."""
        return (
            self.lat_min <= lat <= self.lat_max
            and self.lon_min <= lon <= self.lon_max
        )
