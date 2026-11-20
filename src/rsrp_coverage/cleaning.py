"""Очистка замеров.

Порядок проверок задан уточнениями к ТЗ:
    1) обязательные поля на месте;
    2) числа разбираются и конечны (без NaN/Infinity);
    3) RSRP в диапазоне [-140, -44];
    4) timestamp — корректный ISO 8601;
    5) координаты внутри района;
    6) дедупликация по четырём нормализованным полям.

dropped_rows — количество УНИКАЛЬНЫХ отброшенных строк, включая пустые,
а не сумма срабатываний отдельных фильтров.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import RSRP_MAX_DBM, RSRP_MIN_DBM, Bounds

REQUIRED_FIELDS = ("lat", "lon", "rsrp_dbm", "timestamp")


@dataclass(frozen=True)
class Measurement:
    lat: float
    lon: float
    rsrp_dbm: float
    timestamp: datetime


@dataclass
class CleaningReport:
    """Сколько строк прочитано, сколько отброшено и по какой причине."""

    total_rows: int = 0
    kept: int = 0
    dropped_rows: int = 0
    dropped_missing_field: int = 0
    dropped_not_a_number: int = 0
    dropped_rsrp_out_of_range: int = 0
    dropped_bad_timestamp: int = 0
    dropped_out_of_bounds: int = 0
    dropped_duplicate: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_rows": self.total_rows,
            "kept": self.kept,
            "dropped_rows": self.dropped_rows,
            "by_reason": {
                "missing_field": self.dropped_missing_field,
                "not_a_number": self.dropped_not_a_number,
                "rsrp_out_of_range": self.dropped_rsrp_out_of_range,
                "bad_timestamp": self.dropped_bad_timestamp,
                "out_of_bounds": self.dropped_out_of_bounds,
                "duplicate": self.dropped_duplicate,
            },
        }


def _parse_float(raw: str | None) -> float | None:
    """Строку в конечное число. NaN, Infinity и мусор дают None."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _parse_timestamp(raw: str | None) -> datetime | None:
    """ISO 8601 в datetime. Всё остальное — None."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def load_measurements(path: str | Path, bounds: Bounds
                      ) -> tuple[list[Measurement], CleaningReport]:
    """Прочитать CSV и вернуть валидные уникальные замеры вместе с отчётом."""
    report = CleaningReport()
    seen: set[tuple[float, float, float, datetime]] = set()
    kept: list[Measurement] = []

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        # Намеренно csv.reader, а не csv.DictReader: DictReader молча
        # проглатывает физически пустые строки, а спека требует считать
        # каждую отброшенную строку, «в том числе пустую».
        reader = csv.reader(handle)
        try:
            header = [name.strip() for name in next(reader)]
        except StopIteration:
            return [], report

        try:
            columns = {field: header.index(field) for field in REQUIRED_FIELDS}
        except ValueError as exc:
            raise ValueError(
                f"В {path} нет обязательной колонки: {exc}. "
                f"Ожидались {', '.join(REQUIRED_FIELDS)}, найдено {', '.join(header)}"
            ) from None

        for row in reader:
            report.total_rows += 1

            # Пустая физическая строка приходит сюда как [] и обязана
            # попасть в dropped_rows, а не исчезнуть.
            if len(row) != len(header):
                report.dropped_missing_field += 1
                report.dropped_rows += 1
                continue

            raw_values = {field: row[index].strip() for field, index in columns.items()}
            if any(value == "" for value in raw_values.values()):
                report.dropped_missing_field += 1
                report.dropped_rows += 1
                continue

            lat = _parse_float(raw_values["lat"])
            lon = _parse_float(raw_values["lon"])
            rsrp = _parse_float(raw_values["rsrp_dbm"])
            if lat is None or lon is None or rsrp is None:
                report.dropped_not_a_number += 1
                report.dropped_rows += 1
                continue

            if not (RSRP_MIN_DBM <= rsrp <= RSRP_MAX_DBM):
                report.dropped_rsrp_out_of_range += 1
                report.dropped_rows += 1
                continue

            stamp = _parse_timestamp(raw_values["timestamp"])
            if stamp is None:
                report.dropped_bad_timestamp += 1
                report.dropped_rows += 1
                continue

            if not bounds.contains(lat, lon):
                report.dropped_out_of_bounds += 1
                report.dropped_rows += 1
                continue

            key = (lat, lon, rsrp, stamp)
            if key in seen:
                report.dropped_duplicate += 1
                report.dropped_rows += 1
                continue
            seen.add(key)
            kept.append(Measurement(lat=lat, lon=lon, rsrp_dbm=rsrp, timestamp=stamp))

    report.kept = len(kept)
    return kept, report
