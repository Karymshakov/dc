"""Проверки бонусного сравнения покрытия днём и ночью.

Проверяются правила, а не реализация:
    * день — полуинтервал [07:00, 19:00) по часам, записанным в самой строке;
    * половины суток агрегируются независимо, медианы не смешиваются;
    * клетка, не прошедшая отсечку по минимуму замеров в одной из половин,
      не выдаётся за разницу покрытия;
    * пустая половина не роняет прогон.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from rsrp_coverage.cleaning import Measurement, load_measurements
from rsrp_coverage.config import Bounds
from rsrp_coverage.daynight import (DAY_END_HOUR, DAY_START_HOUR, compare_day_night,
                                    is_daytime, split_day_night)
from rsrp_coverage.geo import cell_center, cell_id
from rsrp_coverage.grid import build_grid

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "07_coverage"
real_data = pytest.mark.skipif(
    not (DATA_DIR / "measurements.csv").exists(),
    reason="нет открытого набора данных",
)


@pytest.fixture
def bounds() -> Bounds:
    return Bounds(
        lat_min=42.84, lat_max=42.87597286423675,
        lon_min=74.56, lon_max=74.60905912302184,
        grid_origin_lat=42.84, grid_origin_lon=74.56,
        cell_size_m=200.0, earth_radius_m=6371000.0, timezone="Asia/Bishkek",
    )


@pytest.fixture(scope="module")
def real_measurements() -> tuple[Bounds, list[Measurement]]:
    """Открытый набор организаторов после штатной очистки."""
    real_bounds = Bounds.from_json(DATA_DIR / "bounds.json")
    measurements, _ = load_measurements(DATA_DIR / "measurements.csv", real_bounds)
    return real_bounds, measurements


def cell_measurements(bounds: Bounds, ix: int, iy: int, hour: int,
                      values: list[float]) -> list[Measurement]:
    """Замеры в центре клетки (ix, iy), снятые в заданный локальный час."""
    lat, lon = cell_center(ix, iy, bounds)
    stamp = datetime.fromisoformat(f"2026-11-16T{hour:02d}:30:00+06:00")
    return [Measurement(lat=lat, lon=lon, rsrp_dbm=value, timestamp=stamp)
            for value in values]


@pytest.mark.parametrize("text,is_day", [
    ("2026-11-16T00:00:00+06:00", False),
    ("2026-11-16T06:59:59+06:00", False),
    ("2026-11-16T07:00:00+06:00", True),
    ("2026-11-16T12:00:00+06:00", True),
    ("2026-11-16T18:59:59+06:00", True),
    ("2026-11-16T19:00:00+06:00", False),
    ("2026-11-16T23:59:59+06:00", False),
])
def test_day_window_is_half_open(text: str, is_day: bool) -> None:
    """День — [07:00, 19:00): 07:00:00 уже день, 19:00:00 уже ночь."""
    assert is_daytime(datetime.fromisoformat(text)) is is_day


def test_every_hour_falls_into_exactly_one_half() -> None:
    """Все 24 часа разложены по половинам, и половины равны по 12 часов."""
    day_hours = {
        hour for hour in range(24)
        if is_daytime(datetime.fromisoformat(f"2026-11-16T{hour:02d}:00:00+06:00"))
    }
    assert day_hours == set(range(DAY_START_HOUR, DAY_END_HOUR))
    assert len(day_hours) == 12


def test_classification_reads_the_written_wall_clock() -> None:
    """Час берётся из смещения самой строки, а не из момента в UTC.

    18:00 с нулевым смещением — тот же момент, что 00:00 следующего дня в
    +06:00. Правило смотрит на записанные настенные часы, поэтому это день;
    код, который жёстко переводил бы время в UTC+6, назвал бы это ночью.
    """
    assert is_daytime(datetime.fromisoformat("2026-11-16T18:00:00+00:00")) is True
    assert is_daytime(datetime.fromisoformat("2026-11-16T07:30:00+06:00")) is True
    assert is_daytime(datetime.fromisoformat("2026-11-16T02:00:00+03:00")) is False


def test_naive_timestamp_is_treated_as_local() -> None:
    """Строка без смещения трактуется как уже локальная: других часов нет."""
    assert is_daytime(datetime(2026, 11, 16, 8, 0)) is True
    assert is_daytime(datetime(2026, 11, 16, 3, 0)) is False


def test_split_is_a_partition_of_the_input(bounds: Bounds) -> None:
    """Каждый замер попадает ровно в одну половину, порядок сохраняется."""
    hourly = [cell_measurements(bounds, 1, 1, hour, [-100.0])[0] for hour in range(24)]
    day, night = split_day_night(hourly)

    assert len(day) + len(night) == len(hourly)
    assert [m.timestamp.hour for m in day] == list(range(DAY_START_HOUR, DAY_END_HOUR))
    assert len(night) == 24 - (DAY_END_HOUR - DAY_START_HOUR)
    assert {m.timestamp.hour for m in day}.isdisjoint(
        {m.timestamp.hour for m in night})


def test_halves_are_aggregated_independently(bounds: Bounds) -> None:
    """У каждой половины своя сетка: замеры разных половин не смешиваются.

    В одной клетке пять дневных замеров по -100 и пять ночных по -120.
    Единая сетка дала бы одну клетку с медианой -110, то есть красную в
    любое время. Независимый счёт даёт жёлтую клетку днём и красную ночью —
    ровно ту разницу покрытия, которую ищет бонус.
    """
    target = cell_id(4, 4)
    measurements = (cell_measurements(bounds, 4, 4, 12, [-100.0] * 5)
                    + cell_measurements(bounds, 4, 4, 23, [-120.0] * 5))

    merged = build_grid(measurements, bounds)
    assert [cell.rsrp_median for cell in merged] == [-110.0]

    comparison = compare_day_night(measurements, bounds)
    assert comparison.day.median_by_cell() == {target: -100.0}
    assert comparison.night.median_by_cell() == {target: -120.0}
    assert [cell.sample_count for cell in comparison.day.cells] == [5]
    assert [cell.sample_count for cell in comparison.night.cells] == [5]
    assert comparison.weak_only_at_night == (target,)
    assert comparison.weak_only_at_day == ()
    assert comparison.weak_in_both == ()


def test_weak_only_at_day_is_symmetric(bounds: Bounds) -> None:
    """Обратный случай: красная днём и жёлтая ночью попадает в weak_only_at_day."""
    target = cell_id(5, 5)
    measurements = (cell_measurements(bounds, 5, 5, 9, [-120.0] * 5)
                    + cell_measurements(bounds, 5, 5, 3, [-100.0] * 5))

    comparison = compare_day_night(measurements, bounds)
    assert comparison.weak_only_at_day == (target,)
    assert comparison.weak_only_at_night == ()


def test_cell_missing_from_one_half_is_not_called_a_difference(bounds: Bounds) -> None:
    """Клетка без отсечки в одной половине не выдаётся за ухудшение покрытия.

    Ночью пять замеров по -120, клетка красная. Днём всего три замера, то
    есть днём клетка не учтена и сравнивать не с чем. Такой случай обязан
    попасть в отдельный список, а не в weak_only_at_night.
    """
    target = cell_id(2, 2)
    measurements = (cell_measurements(bounds, 2, 2, 23, [-120.0] * 5)
                    + cell_measurements(bounds, 2, 2, 12, [-120.0] * 3))

    comparison = compare_day_night(measurements, bounds)
    assert comparison.day.cells_counted == 0
    assert comparison.night.weak_ids == {target}
    assert comparison.weak_only_at_night == ()
    assert comparison.weak_at_night_not_counted_at_day == (target,)
    assert comparison.cells_in_both_halves == 0


def test_median_of_medians_is_taken_over_cells(bounds: Bounds) -> None:
    """Медиана медиан считается по клеткам, а не по всем замерам половины.

    Клетка A: пять замеров по -100, медиана -100. Клетка B: девять замеров
    по -120, медиана -120. Медиана медиан — -110, тогда как медиана по всем
    четырнадцати замерам дала бы -120.
    """
    measurements = (cell_measurements(bounds, 1, 1, 12, [-100.0] * 5)
                    + cell_measurements(bounds, 6, 6, 12, [-120.0] * 9))

    comparison = compare_day_night(measurements, bounds)
    assert comparison.day.cells_counted == 2
    assert comparison.day.median_of_medians == pytest.approx(-110.0)


def test_median_shift_uses_only_cells_present_in_both_halves(bounds: Bounds) -> None:
    """Сдвиг «ночь минус день» считается по клеткам из обеих половин.

    Общая клетка ухудшается на 4 дБ. Клетка, учтённая только ночью,
    в сдвиг не входит: сравнивать её не с чем.
    """
    measurements = (cell_measurements(bounds, 1, 1, 12, [-100.0] * 5)
                    + cell_measurements(bounds, 1, 1, 23, [-104.0] * 5)
                    + cell_measurements(bounds, 7, 7, 23, [-130.0] * 5))

    comparison = compare_day_night(measurements, bounds)
    assert comparison.cells_in_both_halves == 1
    assert comparison.median_shift_db == pytest.approx(-4.0)
    assert comparison.median_abs_shift_db == pytest.approx(4.0)


@pytest.mark.parametrize("hour,filled_half", [(12, "day"), (23, "night")])
def test_empty_half_does_not_break_the_run(bounds: Bounds, hour: int,
                                           filled_half: str) -> None:
    """Если замеры попали только в одну половину, прогон обязан дойти до конца."""
    comparison = compare_day_night(
        cell_measurements(bounds, 1, 1, hour, [-120.0] * 5), bounds)
    filled = getattr(comparison, filled_half)
    empty = comparison.night if filled_half == "day" else comparison.day

    assert filled.cells_counted == 1
    assert len(filled.weak) == 1
    assert empty.measurement_count == 0
    assert empty.cells_counted == 0
    assert empty.weak == ()
    assert empty.median_of_medians is None
    assert comparison.cells_in_both_halves == 0
    assert comparison.median_shift_db is None
    assert comparison.weak_only_at_night == ()
    assert comparison.weak_only_at_day == ()

    payload = comparison.to_json()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_no_measurements_at_all_gives_empty_comparison(bounds: Bounds) -> None:
    """Пустой вход — пустое сравнение, а не исключение."""
    comparison = compare_day_night([], bounds)

    assert comparison.day.measurement_count == 0
    assert comparison.night.measurement_count == 0
    assert comparison.day.median_of_medians is None
    assert comparison.night.median_of_medians is None
    assert comparison.median_shift_db is None
    assert comparison.median_abs_shift_db is None
    assert comparison.weak_in_both == ()
    assert comparison.to_json()["comparison"]["cells_counted_in_both_halves"] == 0


def test_report_keeps_the_timezone_from_bounds(bounds: Bounds) -> None:
    """Зона из bounds.json идёт в отчёт подписью, окно дня — в локальных часах."""
    comparison = compare_day_night([], bounds)
    payload = comparison.to_json()

    assert payload["timezone"] == "Asia/Bishkek"
    assert payload["day_window_local"] == "07:00-19:00"
    assert set(payload) == {"timezone", "day_window_local", "day", "night",
                            "comparison"}


@real_data
def test_real_split_covers_every_cleaned_measurement(real_measurements) -> None:
    """На открытом наборе обе половины непусты и вместе дают всю выборку."""
    real_bounds, measurements = real_measurements
    day, night = split_day_night(measurements)
    comparison = compare_day_night(measurements, real_bounds)

    assert len(day) + len(night) == len(measurements)
    assert day and night
    assert comparison.day.measurement_count == len(day)
    assert comparison.night.measurement_count == len(night)


@real_data
def test_real_weak_cells_are_split_without_loss_or_overlap(real_measurements) -> None:
    """Каждая красная клетка половины попадает ровно в один список сравнения.

    Красные ночью = красные в обеих половинах + красные только ночью +
    красные ночью, но не учтённые днём. Для дня симметрично. Иначе отчёт
    либо терял бы клетки, либо считал бы их дважды.
    """
    real_bounds, measurements = real_measurements
    comparison = compare_day_night(measurements, real_bounds)
    both = set(comparison.weak_in_both)

    night_parts = [both, set(comparison.weak_only_at_night),
                   set(comparison.weak_at_night_not_counted_at_day)]
    day_parts = [both, set(comparison.weak_only_at_day),
                 set(comparison.weak_at_day_not_counted_at_night)]

    for parts, half in ((night_parts, comparison.night), (day_parts, comparison.day)):
        assert set().union(*parts) == half.weak_ids
        assert sum(len(part) for part in parts) == len(half.weak)


@real_data
def test_real_halves_contain_only_cells_of_the_full_grid(real_measurements) -> None:
    """Деление пополам не создаёт клеток, которых нет в полной сетке.

    Обратное неверно: у половины замеров вдвое меньше, и часть клеток не
    проходит отсечку по минимуму. Поэтому проверяется вложенность, а не
    равенство.
    """
    real_bounds, measurements = real_measurements
    full = {cell.cell_id for cell in build_grid(measurements, real_bounds)}
    comparison = compare_day_night(measurements, real_bounds)

    assert comparison.day.cell_ids <= full
    assert comparison.night.cell_ids <= full
    assert comparison.day.cells_counted < len(full)
    assert comparison.night.cells_counted < len(full)
