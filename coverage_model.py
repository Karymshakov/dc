from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from math import asin, cos, degrees, floor, radians, sin, sqrt
from statistics import median
from typing import Iterable, TypedDict


Point = tuple[float, float]
MIN_TOWER_DISTANCE_M = 500
COVERAGE_RADIUS_M = 1000


@dataclass(frozen=True)
class Grid:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    grid_origin_lat: float
    grid_origin_lon: float
    cell_size_m: float
    earth_radius_m: float

    @property
    def bounds(self) -> tuple[Point, Point]:
        return (self.lat_min, self.lon_min), (self.lat_max, self.lon_max)

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    def index(self, lat: float, lon: float) -> tuple[int, int]:
        x = self._longitude_scale * radians(lon - self.grid_origin_lon)
        y = self.earth_radius_m * radians(lat - self.grid_origin_lat)
        return floor(x / self.cell_size_m), floor(y / self.cell_size_m)

    def cell_bounds(self, ix: int, iy: int) -> tuple[Point, Point]:
        return self._point_at(ix, iy), self._point_at(ix + 1, iy + 1)

    def cell_center(self, ix: int, iy: int) -> Point:
        return self._point_at(ix + 0.5, iy + 0.5)

    @property
    def _longitude_scale(self) -> float:
        return self.earth_radius_m * cos(radians(self.grid_origin_lat))

    def _point_at(self, x: float, y: float) -> Point:
        lat = self.grid_origin_lat + degrees(y * self.cell_size_m / self.earth_radius_m)
        lon = self.grid_origin_lon + degrees(x * self.cell_size_m / self._longitude_scale)
        return lat, lon


@dataclass(frozen=True)
class Measurement:
    lat: float
    lon: float
    rsrp_dbm: float
    timestamp: datetime


@dataclass(frozen=True)
class Cell:
    ix: int
    iy: int
    median: float
    count: int

    @property
    def id(self) -> str:
        return f"X{self.ix:03d}_Y{self.iy:03d}"

    @property
    def state(self) -> str:
        if self.median >= -90:
            return "strong"
        if self.median >= -105:
            return "fair"
        return "weak"


@dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lon: float

    @property
    def point(self) -> Point:
        return self.lat, self.lon


@dataclass(frozen=True)
class Candidate:
    site: Site
    nearest_m: float | None = None

    @property
    def eligible(self) -> bool:
        return self.nearest_m is None or self.nearest_m >= MIN_TOWER_DISTANCE_M


def aggregate(measurements: Iterable[Measurement], grid: Grid) -> list[Cell]:
    readings = defaultdict(list)
    for measurement in measurements:
        index = grid.index(measurement.lat, measurement.lon)
        readings[index].append(measurement.rsrp_dbm)

    return [
        Cell(ix, iy, median(values), len(values))
        for (ix, iy), values in sorted(readings.items())
        if len(values) >= 5
    ]


def haversine(point_a: Point, point_b: Point, radius: float) -> float:
    lat_a, lon_a = point_a
    lat_b, lon_b = point_b
    half_lat = radians(lat_b - lat_a) / 2
    half_lon = radians(lon_b - lon_a) / 2
    chord = sin(half_lat) ** 2 + cos(radians(lat_a)) * cos(radians(lat_b)) * sin(half_lon) ** 2
    return 2 * radius * asin(sqrt(min(1.0, chord)))


def assess_candidates(
    sites: Iterable[Site], towers: Iterable[Site], grid: Grid
) -> list[Candidate]:
    tower_points = [tower.point for tower in towers]
    candidates = []
    for site in sites:
        distances = (haversine(site.point, point, grid.earth_radius_m) for point in tower_points)
        candidates.append(Candidate(site, min(distances, default=None)))
    return candidates


def eligible_coverage(
    candidates: Iterable[Candidate], cells: Iterable[Cell], grid: Grid
) -> dict[str, set[str]]:
    return eligible_coverage_by_radius(candidates, cells, grid, (COVERAGE_RADIUS_M,))[COVERAGE_RADIUS_M]


def eligible_coverage_by_radius(
    candidates: Iterable[Candidate], cells: Iterable[Cell], grid: Grid, radii_m: Iterable[int]
) -> dict[int, dict[str, set[str]]]:
    weak_centers = [
        (cell.id, grid.cell_center(cell.ix, cell.iy)) for cell in cells if cell.state == "weak"
    ]
    scenarios: dict[int, dict[str, set[str]]] = {radius: {} for radius in radii_m}
    for candidate in candidates:
        if not candidate.eligible:
            continue
        point = candidate.site.point
        distances = [
            (cell_id, haversine(point, center, grid.earth_radius_m))
            for cell_id, center in weak_centers
        ]
        for radius, coverage in scenarios.items():
            coverage[candidate.site.id] = {cell_id for cell_id, distance in distances if distance <= radius}
    return scenarios


def rank_pairs(coverage: dict[str, set[str]]) -> list[tuple[tuple[str, str], int]]:
    ranked = [
        ((left, right), len(coverage[left] | coverage[right]))
        for left, right in combinations(sorted(coverage), 2)
    ]
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


class ResilienceOption(TypedDict):
    ids: list[str]
    covered_weak_cells: int
    worst_single_site_cells: int
    single_site_cells: dict[str, int]


def resilience_front(
    coverage: dict[str, set[str]],
    ranking: Iterable[tuple[tuple[str, str], int]],
) -> list[ResilienceOption]:
    single_counts = {site: len(cells) for site, cells in coverage.items()}
    options: list[ResilienceOption] = [
        {
            "ids": list(pair),
            "covered_weak_cells": total,
            "worst_single_site_cells": min(single_counts[site] for site in pair),
            "single_site_cells": {site: single_counts[site] for site in pair},
        }
        for pair, total in ranking
    ]
    options.sort(key=lambda option: (
        -option["covered_weak_cells"], -option["worst_single_site_cells"], option["ids"],
    ))

    # With totals descending, only an improvement in single-site coverage is undominated.
    front = []
    best_single_coverage = -1
    for option in options:
        remaining = option["worst_single_site_cells"]
        if remaining > best_single_coverage:
            front.append(option)
            best_single_coverage = remaining
    return front


def best_pair(coverage: dict[str, set[str]]) -> tuple[str, str]:
    ranked = rank_pairs(coverage)
    if not ranked:
        raise ValueError("Для выбора пары нужны хотя бы две допустимые площадки")
    return ranked[0][0]


def greedy_pair(coverage: dict[str, set[str]]) -> tuple[str, str]:
    remaining = sorted(coverage)
    if len(remaining) < 2:
        raise ValueError("Для выбора пары нужны хотя бы две допустимые площадки")

    first = max(remaining, key=lambda site_id: len(coverage[site_id]))
    remaining.remove(first)
    second = max(remaining, key=lambda site_id: len(coverage[site_id] - coverage[first]))
    return (first, second) if first < second else (second, first)
