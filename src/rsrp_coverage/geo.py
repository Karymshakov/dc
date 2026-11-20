"""Геометрия задания: гаверсинус и локальная равнопромежуточная сетка.

Проекция — НЕ Web Mercator. Формулы заданы уточнениями к ТЗ:
    x = R * cos(lat0) * rad(lon - lon0)
    y = R * rad(lat - lat0)
    cell = (floor(x / cell_size), floor(y / cell_size))
"""
from __future__ import annotations

import math

from .config import Bounds


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float,
                earth_radius_m: float) -> float:
    """Расстояние по дуге большого круга, метры."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    return 2.0 * earth_radius_m * math.asin(math.sqrt(a))


def project_m(lat: float, lon: float, bounds: Bounds) -> tuple[float, float]:
    """Координаты в метрах относительно начала сетки."""
    lat0 = math.radians(bounds.grid_origin_lat)
    x = bounds.earth_radius_m * math.cos(lat0) * math.radians(lon - bounds.grid_origin_lon)
    y = bounds.earth_radius_m * math.radians(lat - bounds.grid_origin_lat)
    return x, y


def cell_index(lat: float, lon: float, bounds: Bounds) -> tuple[int, int]:
    """Индексы клетки (ix, iy). Замер на верхней границе попадает по правилу floor."""
    x, y = project_m(lat, lon, bounds)
    return (math.floor(x / bounds.cell_size_m), math.floor(y / bounds.cell_size_m))


def cell_id(ix: int, iy: int) -> str:
    """Канонический идентификатор клетки, например X006_Y013."""
    return f"X{ix:03d}_Y{iy:03d}"


def cell_center(ix: int, iy: int, bounds: Bounds) -> tuple[float, float]:
    """Центр клетки обратно в (lat, lon)."""
    lat0 = math.radians(bounds.grid_origin_lat)
    x_c = (ix + 0.5) * bounds.cell_size_m
    y_c = (iy + 0.5) * bounds.cell_size_m
    lat = bounds.grid_origin_lat + math.degrees(y_c / bounds.earth_radius_m)
    lon = bounds.grid_origin_lon + math.degrees(
        x_c / (bounds.earth_radius_m * math.cos(lat0))
    )
    return lat, lon
