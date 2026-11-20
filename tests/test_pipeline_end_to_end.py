"""Сквозные проверки всего пайплайна.

Главный тест здесь сверяет полный прогон с открытым эталоном организаторов.
Без него правила, которые соединяются только в pipeline.py — отсечка 500 м,
выбор худших среди ВСЕХ клеток, покрытие только красных — не закрыты ничем:
их можно сломать, и модульные тесты останутся зелёными.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rsrp_coverage.pipeline import run
from rsrp_coverage.siting import load_sites

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "07_coverage"
pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "measurements.csv").exists(),
    reason="нет открытого набора данных",
)


@pytest.fixture(scope="module")
def result():
    return run(DATA_DIR)


def test_matches_open_reference(result):
    """Полный прогон обязан совпасть с expected.json по всем четырём полям."""
    expected = json.loads((DATA_DIR / "expected.json").read_text(encoding="utf-8"))
    assert result.to_result_json() == expected


def test_control_numbers_from_readme(result):
    """Контрольные числа из README организаторов."""
    assert result.report.total_rows == 5000
    assert result.report.dropped_rows == 200
    assert result.report.kept == 4800
    assert result.cells_with_measurements == 398
    assert len(result.cells) == 387


def test_eligibility_filter_is_actually_applied(result):
    """Четыре кандидата стоят на координатах существующих БС и обязаны выбыть."""
    assert len(result.candidates) == 20
    assert len(result.eligible) == 16
    eligible_ids = {site.id for site in result.eligible}
    assert {"C01", "C02", "C03", "C04"}.isdisjoint(eligible_ids)


def test_chosen_sites_are_eligible(result):
    """Выбранная пара обязана состоять из допустимых площадок."""
    eligible_ids = {site.id for site in result.eligible}
    assert set(result.best_candidates) <= eligible_ids


def test_worst_cells_are_searched_among_all_cells(result):
    """Худшие ищутся среди всех клеток, а не только среди красных."""
    assert len(result.worst) == 3
    medians = [cell.rsrp_median for cell in result.worst]
    assert medians == sorted(medians)
    assert min(cell.rsrp_median for cell in result.cells) == medians[0]


def test_covered_cells_are_weak_and_counted_once(result):
    """covered_weak_cells — размер объединения красных клеток, без двойного учёта."""
    assert 0 < result.covered_weak_cells <= len(result.weak)


def test_exhaustive_is_at_least_as_good_as_greedy(result):
    """Полный перебор не может проиграть жадному. На этих данных он выигрывает."""
    assert result.covered_weak_cells >= result.greedy_covered
    assert result.greedy_candidates != result.best_candidates


def test_result_json_shape(result):
    payload = result.to_result_json()
    assert set(payload) == {"worst_cells", "best_candidates",
                            "dropped_rows", "covered_weak_cells"}
    assert len(payload["best_candidates"]) == 2
    assert payload["best_candidates"] == sorted(payload["best_candidates"])
    for cell in payload["worst_cells"]:
        assert set(cell) == {"cell_id", "rsrp_median"}


def test_load_sites_skips_broken_rows(tmp_path):
    """Мусор в candidates.csv не должен ронять прогон."""
    path = tmp_path / "candidates.csv"
    path.write_text(
        "id,lat,lon\n"
        "C01,42.85,74.58\n"
        "C02,,74.59\n"
        "C03,не-число,74.60\n"
        ",42.86,74.61\n"
        "C01,42.87,74.62\n"
        "C04,42.86,74.61\n",
        encoding="utf-8",
    )
    sites = load_sites(path)
    assert [site.id for site in sites] == ["C01", "C04"]


def test_load_sites_reports_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sites(tmp_path / "нет-такого.csv")


def test_load_sites_reports_missing_columns(tmp_path):
    path = tmp_path / "candidates.csv"
    path.write_text("name,x,y\nC01,42.85,74.58\n", encoding="utf-8")
    with pytest.raises(ValueError, match="обязательных колонок"):
        load_sites(path)
