"""Выбор площадок под новые базовые станции.

Площадка допустима, если расстояние до КАЖДОЙ существующей БС >= 500 м.
Площадка покрывает красную клетку, если расстояние до её центра <= 1000 м.
Пара выбирается полным перебором с максимизацией размера объединения;
при равенстве берётся лексикографически минимальная отсортированная пара ID.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .config import (COVERAGE_RADIUS_M, MIN_DISTANCE_TO_TOWER_M, SITES_TO_PICK,
                     Bounds)
from .geo import haversine_m
from .grid import Cell


@dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lon: float


def load_sites(path: str | Path) -> list[Site]:
    """Прочитать towers.csv или candidates.csv.

    Мусор в этих файлах ТЗ не обещает, но падение с трейсбеком означало бы
    отсутствие result.json и ноль за функциональность. Поэтому нечитаемые
    строки пропускаются, а структурные проблемы дают понятное сообщение.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"Не найден файл площадок: {target}")

    sites: list[Site] = []
    seen_ids: set[str] = set()
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = {"id", "lat", "lon"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"В {target} нет обязательных колонок: {', '.join(sorted(missing))}"
            )
        for row in reader:
            site_id = (row.get("id") or "").strip()
            if not site_id or site_id in seen_ids:
                continue
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            seen_ids.add(site_id)
            sites.append(Site(id=site_id, lat=lat, lon=lon))
    return sites


def eligible_candidates(candidates: list[Site], towers: list[Site],
                        bounds: Bounds) -> list[Site]:
    """Отбросить площадки ближе 500 м хотя бы к одной существующей БС."""
    allowed: list[Site] = []
    for candidate in candidates:
        too_close = any(
            haversine_m(candidate.lat, candidate.lon, tower.lat, tower.lon,
                        bounds.earth_radius_m) < MIN_DISTANCE_TO_TOWER_M
            for tower in towers
        )
        if not too_close:
            allowed.append(candidate)
    return allowed


def coverage_sets(candidates: list[Site], weak: list[Cell],
                  bounds: Bounds) -> dict[str, frozenset[str]]:
    """Для каждой площадки — множество красных клеток в радиусе 1 км."""
    result: dict[str, frozenset[str]] = {}
    for candidate in candidates:
        covered = {
            cell.cell_id
            for cell in weak
            if haversine_m(candidate.lat, candidate.lon,
                           cell.center_lat, cell.center_lon,
                           bounds.earth_radius_m) <= COVERAGE_RADIUS_M
        }
        result[candidate.id] = frozenset(covered)
    return result


def best_pair_exhaustive(coverage: dict[str, frozenset[str]],
                         size: int = SITES_TO_PICK) -> tuple[list[str], int]:
    """Полный перебор пар. Возвращает отсортированную пару ID и размер объединения."""
    best_ids: list[str] | None = None
    best_size = -1
    for combo in combinations(sorted(coverage), size):
        union = frozenset().union(*(coverage[site_id] for site_id in combo))
        candidate_ids = sorted(combo)
        if len(union) > best_size or (len(union) == best_size
                                      and best_ids is not None
                                      and candidate_ids < best_ids):
            best_size = len(union)
            best_ids = candidate_ids
    return (best_ids or []), max(best_size, 0)


def best_pair_greedy(coverage: dict[str, frozenset[str]],
                     size: int = SITES_TO_PICK) -> tuple[list[str], int]:
    """Жадный выбор: каждый шаг берёт площадку с наибольшим приростом.

    Нужен только для бонусного сравнения с полным перебором.
    """
    chosen: list[str] = []
    union: set[str] = set()
    for _ in range(size):
        best_id = None
        best_gain = -1
        for site_id in sorted(coverage):
            if site_id in chosen:
                continue
            gain = len(coverage[site_id] - union)
            if gain > best_gain:
                best_gain = gain
                best_id = site_id
        if best_id is None:
            break
        chosen.append(best_id)
        union |= coverage[best_id]
    return sorted(chosen), len(union)
