"""Проверки того, что реально попадает к жюри: файлы на диске и живая страница.

Эти тесты закрывают класс дыр, найденный адвокатом дьявола: `main.py --expected`
сравнивает payload **в памяти**, а не файл `out/result.json`, и ни один тест
не исполнял сгенерированный JavaScript. В результате проходили мутации, при
которых жюри получило бы бонусный отчёт вместо ответа или пустую белую
страницу — а прогон оставался зелёным.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rsrp_coverage import cli
from rsrp_coverage.mapping import COLORS, build_payload, render_map
from rsrp_coverage.pipeline import run

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "07_coverage"
EXPECTED_KEYS = {"worst_cells", "best_candidates", "dropped_rows", "covered_weak_cells"}

pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "measurements.csv").exists(),
    reason="нет открытого набора данных",
)


@pytest.fixture(scope="module")
def produced(tmp_path_factory):
    """Один настоящий прогон CLI во временный каталог."""
    out_dir = tmp_path_factory.mktemp("out")
    code = cli.main(["--data", str(DATA_DIR), "--out", str(out_dir)])
    assert code == 0
    return out_dir


def test_result_json_on_disk_matches_reference(produced):
    """Жюри читает ФАЙЛ, а не payload в памяти."""
    written = json.loads((produced / "result.json").read_text(encoding="utf-8"))
    expected = json.loads((DATA_DIR / "expected.json").read_text(encoding="utf-8"))
    assert written == expected


def test_result_json_has_exactly_the_jury_keys(produced):
    """Формат жюри не расширяется бонусами."""
    written = json.loads((produced / "result.json").read_text(encoding="utf-8"))
    assert set(written) == EXPECTED_KEYS


def test_bonus_goes_to_its_own_file(produced):
    """Бонус пишется отдельно и не затирает result.json."""
    bonus = json.loads((produced / "bonus_day_night.json").read_text(encoding="utf-8"))
    assert EXPECTED_KEYS.isdisjoint(bonus)
    assert {"day", "night"} <= set(bonus)


def test_bonus_uses_the_same_measurements_as_the_answer(produced):
    """Бонус считается по той же выборке, что и основной ответ.

    CLI читает measurements.csv второй раз. Если очистка разойдётся,
    бонус будет посчитан по другим данным — здесь это станет видно.
    """
    bonus = json.loads((produced / "bonus_day_night.json").read_text(encoding="utf-8"))
    result = run(DATA_DIR)
    halves = bonus["day"]["measurements"] + bonus["night"]["measurements"]
    assert halves == result.report.kept


def test_all_expected_files_are_produced(produced):
    for name in ("result.json", "map.html", "bonus_day_night.json"):
        assert (produced / name).exists(), name


def test_expected_flag_fails_on_mismatch(tmp_path):
    """Флаг самопроверки обязан возвращать ненулевой код при расхождении."""
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"worst_cells": [], "best_candidates": [],
                                 "dropped_rows": 0, "covered_weak_cells": 0}),
                     encoding="utf-8")
    code = cli.main(["--data", str(DATA_DIR), "--out", str(tmp_path / "out"),
                     "--expected", str(wrong)])
    assert code == 1


def test_missing_data_directory_is_reported(tmp_path):
    """Понятная ошибка вместо трейсбека."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--data", str(tmp_path / "нет-такого"), "--out", str(tmp_path)])
    assert exit_info.value.code != 0


# --- Цвета клеток на карте -------------------------------------------------

def test_cell_colours_follow_the_median(tmp_path):
    """Цвет прямоугольника на карте обязан следовать медиане клетки.

    Пороги проверяются отдельно в test_grid_and_siting, но связь
    «медиана -> цвет на карте» до этого не проверялась нигде: можно было
    покрасить всю карту в один цвет, и прогон остался бы зелёным.
    """
    result = run(DATA_DIR)
    payload = build_payload(result)
    by_id = {cell.cell_id: cell for cell in result.cells}

    for drawn in payload["cells"]:
        cell = by_id[drawn["id"]]
        assert drawn["color"] == COLORS[cell.color], drawn["id"]

    drawn_colours = {cell["color"] for cell in payload["cells"]}
    assert drawn_colours == set(COLORS.values()), "на карте обязаны быть все три цвета"


def test_weak_cells_are_drawn_red(tmp_path):
    result = run(DATA_DIR)
    payload = build_payload(result)
    weak_ids = {cell.cell_id for cell in result.weak}
    for drawn in payload["cells"]:
        if drawn["id"] in weak_ids:
            assert drawn["color"] == COLORS["red"], drawn["id"]


# --- Живая страница --------------------------------------------------------

def extract_page_script(html: str) -> str:
    """Последний блок <script> — наш код, до него идёт встроенный Leaflet."""
    blocks = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert blocks, "на странице нет ни одного инлайн-скрипта"
    return blocks[-1]


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_generated_javascript_parses(tmp_path):
    """Сгенерированный JS обязан быть синтаксически корректным.

    Страница собирается подстановкой в шаблон, поэтому опечатка в JS не
    ломает ни один питоновский тест — она превращает карту в пустую белую
    страницу уже у жюри. Здесь скрипт проверяется настоящим движком.
    """
    html = render_map(run(DATA_DIR), tmp_path / "map.html").read_text(encoding="utf-8")
    script = tmp_path / "page.js"
    script.write_text(extract_page_script(html), encoding="utf-8")

    finished = subprocess.run(["node", "--check", str(script)],
                              capture_output=True, text=True)
    assert finished.returncode == 0, finished.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="нет node")
def test_vendored_leaflet_parses(tmp_path):
    """Встроенный Leaflet не побился при выкачивании."""
    vendor = Path(__file__).resolve().parents[1] / "vendor" / "leaflet" / "leaflet.js"
    finished = subprocess.run(["node", "--check", str(vendor)],
                              capture_output=True, text=True)
    assert finished.returncode == 0, finished.stderr


def test_marker_icons_are_set_explicitly(tmp_path):
    """Иконки маркеров заданы явно, а не угадываются эвристикой Leaflet.

    После инлайна CSS эвристика _detectIconPath не может отрезать
    'marker-icon.png' от data: URI и ищет картинку рядом со страницей,
    где её нет, — три маркера худших клеток стали бы битыми.
    """
    html = render_map(run(DATA_DIR), tmp_path / "map.html").read_text(encoding="utf-8")
    assert "L.Icon.Default.prototype.options.iconUrl" in html
    assert 'L.Icon.Default.imagePath = ""' in html
    for option in ("iconUrl", "iconRetinaUrl", "shadowUrl"):
        pattern = rf'{option} = "data:image/png;base64,'
        assert re.search(pattern, html), option


def test_no_placeholders_survive(tmp_path):
    html = render_map(run(DATA_DIR), tmp_path / "map.html").read_text(encoding="utf-8")
    assert re.findall(r"__[A-ZА-Я_0-9]+__", html) == []
