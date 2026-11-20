"""Бонус ТЗ: сравнение покрытия днём и ночью.

Границы дня
-----------
ТЗ границу не задаёт, поэтому она выбрана здесь и обоснована:
**день — полуинтервал [07:00, 19:00) по локальному времени, ночь — всё
остальное.**

1. Граница по профилю нагрузки, а не по восходу и закату. Сравнение
   день/ночь нужно, чтобы увидеть покрытие при разной загрузке сети: днём
   соты заняты абонентами, ночью разгружены, и слабые зоны видны «в чистом
   виде». В Бишкеке в ноябре солнце встаёт около 07:20 и садится около
   17:20, но сеть начинает грузиться с утренним часом пик и разгружается
   только к позднему вечеру, так что астрономическая граница здесь не
   полезна.
2. Половины равны: 12 часов против 12 часов. Замеры в открытом наборе
   распределены по часам почти равномерно, поэтому выборки выходят
   сопоставимыми (2403 замера против 2397) и разницу медиан можно читать
   как разницу покрытия, а не как разницу размеров выборки.
3. Целые часы: границу можно пересказать словами и проверить вручную.
4. Полуинтервал, а не отрезок: 07:00:00 — уже день, 19:00:00 — уже ночь.
   Каждый момент попадает ровно в одну половину, сумма половин равна всей
   выборке. Нижняя граница включена — то же правило, что у клеток сетки,
   где `floor` относит точку к клетке с меньшим индексом.

Константы лежат в этом модуле, а не в `config.py`: это не числа спеки, по
которым сверяется жюри, а выбор авторов решения для бонусного отчёта.

Локальное время
---------------
`Measurement.timestamp` разбирается `datetime.fromisoformat`, то есть несёт
смещение, записанное в самих данных (в открытом наборе — `+06:00` у всех
4800 принятых замеров). Локальный час — это час настенных часов в этом
смещении, то есть `timestamp.hour`. Арифметики со смещением мы не делаем и
число «+6» в коде не фиксируем: правило звучит как «час, который записан в
строке», и оно не изменится, если организаторы выгрузят скрытый набор с
другим смещением.

`zoneinfo` намеренно не используется. Перевод момента в `Asia/Bishkek`
нужен только тогда, когда данные записаны в чужой зоне, а за него пришлось
бы заплатить зависимостью: на Windows база IANA в стандартной библиотеке
отсутствует, и `ZoneInfo("Asia/Bishkek")` там падает без пакета `tzdata` —
то есть решение перестало бы запускаться у части жюри. `bounds.timezone`
поэтому идёт в отчёт подписью: он обязан совпадать с зоной, в которой
записаны timestamp'ы, и для открытого набора совпадает (Asia/Bishkek —
UTC+6 круглый год, перехода на летнее время нет с 2005 года).

Наивный timestamp без смещения трактуется как уже локальный: других часов
у такой строки нет.

Что считается
-------------
Каждая половина суток проходит через те же `build_grid`/`weak_cells`, что и
основной прогон, и НЕЗАВИСИМО от другой половины: отсечка «не меньше пяти
замеров в клетке» применяется к половине, а не к полной выборке. Поэтому
клетка может быть учтена в одной половине и выпасть из другой — такие
случаи выделены в отдельные списки и не выдаются за ухудшение покрытия.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

from .cleaning import Measurement
from .config import Bounds
from .grid import Cell, build_grid, weak_cells

# Границы дня, локальные часы. День — [DAY_START_HOUR, DAY_END_HOUR).
DAY_START_HOUR = 7
DAY_END_HOUR = 19


def day_window_label() -> str:
    """Окно дня строкой для отчёта, например 07:00-19:00."""
    return f"{DAY_START_HOUR:02d}:00-{DAY_END_HOUR:02d}:00"


def local_hour(moment: datetime) -> int:
    """Час настенных часов в том смещении, в котором записан замер.

    Смещение берётся из самого timestamp, поэтому никакой зоны знать не
    нужно. Наивный timestamp считается уже локальным.
    """
    return moment.hour


def is_daytime(moment: datetime) -> bool:
    """День — полуинтервал [07:00, 19:00): 07:00:00 день, 19:00:00 уже ночь."""
    return DAY_START_HOUR <= local_hour(moment) < DAY_END_HOUR


def split_day_night(
    measurements: list[Measurement],
) -> tuple[list[Measurement], list[Measurement]]:
    """Разделить замеры на дневные и ночные. Порядок внутри половин сохраняется."""
    day: list[Measurement] = []
    night: list[Measurement] = []
    for item in measurements:
        if is_daytime(item.timestamp):
            day.append(item)
        else:
            night.append(item)
    return day, night


def _round_db(value: float | None) -> float | None:
    """Децибелы в отчёт — до двух знаков.

    Исходный RSRP задан с одним знаком, медиана чётного числа значений
    добавляет ровно ещё один. Всё, что ниже, — шум двоичного сложения.
    Числа основного `result.json` это округление не затрагивает.
    """
    return None if value is None else round(value, 2)


@dataclass(frozen=True)
class HalfSummary:
    """Сводка по одной половине суток: своя сетка, свои красные клетки."""

    label: str
    measurement_count: int
    cells: tuple[Cell, ...]
    weak: tuple[Cell, ...]
    median_of_medians: float | None

    @property
    def cells_counted(self) -> int:
        """Сколько клеток учтено, то есть прошло отсечку по минимуму замеров."""
        return len(self.cells)

    @property
    def cell_ids(self) -> frozenset[str]:
        return frozenset(cell.cell_id for cell in self.cells)

    @property
    def weak_ids(self) -> frozenset[str]:
        return frozenset(cell.cell_id for cell in self.weak)

    def median_by_cell(self) -> dict[str, float]:
        """Медиана RSRP по каждой учтённой клетке этой половины."""
        return {cell.cell_id: cell.rsrp_median for cell in self.cells}

    def as_line(self) -> str:
        """Одна строка для stdout."""
        median_text = ("нет учтённых клеток" if self.median_of_medians is None
                       else f"{self.median_of_medians:.2f}")
        return (f"{self.measurement_count} замеров, {self.cells_counted} клеток, "
                f"{len(self.weak)} красных, медиана медиан {median_text}")

    def to_json(self) -> dict:
        return {
            "measurements": self.measurement_count,
            "cells_counted": self.cells_counted,
            "weak_cells": len(self.weak),
            "median_of_medians_dbm": _round_db(self.median_of_medians),
        }


@dataclass(frozen=True)
class DayNightComparison:
    """Сравнение двух половин суток.

    `weak_only_at_night` и `weak_only_at_day` — клетки, учтённые в ОБЕИХ
    половинах, но красные только в одной: это и есть разница покрытия.
    Клетки, красные в одной половине и не прошедшие отсечку по минимуму
    замеров в другой, сравнению не подлежат и лежат в отдельных списках
    `weak_at_night_not_counted_at_day` и `weak_at_day_not_counted_at_night`.
    """

    timezone: str
    day_window: str
    day: HalfSummary
    night: HalfSummary
    cells_in_both_halves: int
    weak_in_both: tuple[str, ...]
    weak_only_at_night: tuple[str, ...]
    weak_only_at_day: tuple[str, ...]
    weak_at_night_not_counted_at_day: tuple[str, ...]
    weak_at_day_not_counted_at_night: tuple[str, ...]
    median_shift_db: float | None
    median_abs_shift_db: float | None

    def to_json(self) -> dict:
        """Содержимое out/bonus_day_night.json.

        Отдельный файл: формат `result.json`, который сверяет жюри, этот
        бонус не затрагивает.
        """
        return {
            "timezone": self.timezone,
            "day_window_local": self.day_window,
            "day": self.day.to_json(),
            "night": self.night.to_json(),
            "comparison": {
                "cells_counted_in_both_halves": self.cells_in_both_halves,
                "median_shift_night_minus_day_db": _round_db(self.median_shift_db),
                "median_abs_shift_db": _round_db(self.median_abs_shift_db),
                "weak_in_both": list(self.weak_in_both),
                "weak_only_at_night": list(self.weak_only_at_night),
                "weak_only_at_day": list(self.weak_only_at_day),
                "weak_at_night_not_counted_at_day":
                    list(self.weak_at_night_not_counted_at_day),
                "weak_at_day_not_counted_at_night":
                    list(self.weak_at_day_not_counted_at_night),
            },
        }


def summarize_half(label: str, measurements: list[Measurement],
                   bounds: Bounds) -> HalfSummary:
    """Построить сетку по одной половине суток.

    Половина считается сама по себе: отсечка по минимуму замеров применяется
    к её собственным замерам. Пустая половина даёт нулевые счётчики и
    `median_of_medians is None`, а не исключение.
    """
    cells = build_grid(measurements, bounds)
    medians = [cell.rsrp_median for cell in cells]
    return HalfSummary(
        label=label,
        measurement_count=len(measurements),
        cells=tuple(cells),
        weak=tuple(weak_cells(cells)),
        median_of_medians=float(median(medians)) if medians else None,
    )


def compare_day_night(measurements: list[Measurement],
                      bounds: Bounds) -> DayNightComparison:
    """Сравнить покрытие днём и ночью по уже очищенным замерам."""
    day_part, night_part = split_day_night(measurements)
    day = summarize_half(f"день {day_window_label()}", day_part, bounds)
    night = summarize_half("ночь остальное время", night_part, bounds)

    shared = day.cell_ids & night.cell_ids
    day_median = day.median_by_cell()
    night_median = night.median_by_cell()
    # Сдвиг считается только по клеткам, учтённым в обеих половинах: иначе
    # сравнивались бы разные наборы клеток.
    shifts = [night_median[cell_id] - day_median[cell_id]
              for cell_id in sorted(shared)]
    median_shift = float(median(shifts)) if shifts else None
    median_abs_shift = float(median(abs(shift) for shift in shifts)) if shifts else None

    return DayNightComparison(
        timezone=bounds.timezone,
        day_window=day_window_label(),
        day=day,
        night=night,
        cells_in_both_halves=len(shared),
        weak_in_both=tuple(sorted(day.weak_ids & night.weak_ids)),
        weak_only_at_night=tuple(sorted((night.weak_ids - day.weak_ids)
                                        & day.cell_ids)),
        weak_only_at_day=tuple(sorted((day.weak_ids - night.weak_ids)
                                      & night.cell_ids)),
        weak_at_night_not_counted_at_day=tuple(sorted(night.weak_ids - day.cell_ids)),
        weak_at_day_not_counted_at_night=tuple(sorted(day.weak_ids - night.cell_ids)),
        median_shift_db=median_shift,
        median_abs_shift_db=median_abs_shift,
    )
