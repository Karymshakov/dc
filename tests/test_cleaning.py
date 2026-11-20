"""Проверки очистки: каждое правило отбрасывания и подсчёт dropped_rows."""
from __future__ import annotations

import pytest

from rsrp_coverage.cleaning import load_measurements
from rsrp_coverage.config import Bounds

HEADER = "lat,lon,rsrp_dbm,timestamp\n"
GOOD = "42.850000,74.580000,-98.2,2026-11-16T13:58:30+06:00\n"


@pytest.fixture
def bounds() -> Bounds:
    return Bounds(
        lat_min=42.84, lat_max=42.87597286423675,
        lon_min=74.56, lon_max=74.60905912302184,
        grid_origin_lat=42.84, grid_origin_lon=74.56,
        cell_size_m=200.0, earth_radius_m=6371000.0, timezone="Asia/Bishkek",
    )


def write_csv(tmp_path, body: str):
    path = tmp_path / "measurements.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def test_keeps_a_valid_row(tmp_path, bounds):
    kept, report = load_measurements(write_csv(tmp_path, GOOD), bounds)
    assert len(kept) == 1
    assert report.dropped_rows == 0


def test_drops_row_of_empty_fields(tmp_path, bounds):
    """Строка вида ,,, — четыре пустых поля."""
    kept, report = load_measurements(write_csv(tmp_path, GOOD + ",,,\n"), bounds)
    assert len(kept) == 1
    assert report.dropped_rows == 1
    assert report.dropped_missing_field == 1


def test_drops_physically_empty_line(tmp_path, bounds):
    """Физически пустая строка тоже попадает в dropped_rows.

    Правило из уточнений: «dropped_rows включает каждую отброшенную строку,
    в том числе пустую». csv.DictReader такие строки молча проглатывает,
    поэтому разбор идёт через csv.reader — этот тест закрывает регрессию.
    """
    body = GOOD + "\n" + "42.851000,74.581000,-99.2,2026-11-16T13:58:31+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 2
    assert report.total_rows == 3
    assert report.dropped_rows == 1


def test_drops_row_with_wrong_column_count(tmp_path, bounds):
    body = GOOD + "42.85,74.58,-98.2\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_rows == 1


def test_file_with_only_header_yields_nothing(tmp_path, bounds):
    kept, report = load_measurements(write_csv(tmp_path, ""), bounds)
    assert kept == []
    assert report.total_rows == 0
    assert report.dropped_rows == 0


def test_drops_missing_single_field(tmp_path, bounds):
    body = GOOD + ",74.603840,-88.9,2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_missing_field == 1


def test_drops_non_numeric(tmp_path, bounds):
    body = GOOD + "42.85,74.58,не-число,2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_not_a_number == 1


def test_drops_nan_and_infinity(tmp_path, bounds):
    body = (GOOD
            + "42.85,74.58,NaN,2026-11-17T14:00:49+06:00\n"
            + "42.85,74.58,Infinity,2026-11-17T14:00:50+06:00\n")
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_not_a_number == 2


@pytest.mark.parametrize("rsrp", ["-140.0", "-44.0"])
def test_rsrp_boundaries_are_inclusive(tmp_path, bounds, rsrp):
    body = f"42.85,74.58,{rsrp},2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_rsrp_out_of_range == 0


@pytest.mark.parametrize("rsrp", ["-140.1", "-43.9", "0", "-200"])
def test_rsrp_outside_range_is_dropped(tmp_path, bounds, rsrp):
    body = f"42.85,74.58,{rsrp},2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert kept == []
    assert report.dropped_rsrp_out_of_range == 1


def test_drops_bad_timestamp(tmp_path, bounds):
    body = GOOD + "42.85,74.58,-98.2,32.13.2026 99:99\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_bad_timestamp == 1


def test_drops_out_of_bounds(tmp_path, bounds):
    body = GOOD + "43.90,74.58,-98.2,2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_out_of_bounds == 1


def test_bounds_are_inclusive(tmp_path, bounds):
    body = (f"{bounds.lat_min},{bounds.lon_min},-98.2,2026-11-17T14:00:49+06:00\n"
            f"{bounds.lat_max},{bounds.lon_max},-98.2,2026-11-17T14:00:50+06:00\n")
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 2
    assert report.dropped_out_of_bounds == 0


def test_duplicate_is_dropped_once(tmp_path, bounds):
    kept, report = load_measurements(write_csv(tmp_path, GOOD + GOOD), bounds)
    assert len(kept) == 1
    assert report.dropped_duplicate == 1


def test_duplicate_detected_across_number_formatting(tmp_path, bounds):
    """Одно и то же число, записанное иначе, — тот же замер."""
    body = GOOD + "42.85,74.58,-98.20,2026-11-16T13:58:30+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 1
    assert report.dropped_duplicate == 1


def test_rows_differing_in_one_field_are_not_duplicates(tmp_path, bounds):
    body = GOOD + "42.850000,74.580000,-98.3,2026-11-16T13:58:30+06:00\n"
    kept, _ = load_measurements(write_csv(tmp_path, body), bounds)
    assert len(kept) == 2


def test_dropped_rows_counts_each_row_once(tmp_path, bounds):
    """Строка, нарушающая сразу два правила, считается один раз."""
    body = GOOD + "99.0,99.0,-500,не-дата\n"
    _, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert report.dropped_rows == 1


def test_total_is_consistent(tmp_path, bounds):
    body = GOOD + GOOD + ",,,\n" + "42.85,74.58,0,2026-11-17T14:00:49+06:00\n"
    kept, report = load_measurements(write_csv(tmp_path, body), bounds)
    assert report.total_rows == len(kept) + report.dropped_rows
