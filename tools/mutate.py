#!/usr/bin/env python3
"""Мутационная проверка: доказывает, что тесты действительно проверяют правила.

Зачем это нужно. Сверка с открытым эталоном — ненадёжный оракул: из 20 поломок
отдельных правил спеки 10 не замечали ни тесты, ни `expected.json`. Среди
выживших была полностью выключенная отсечка 500 м.

Как работает. Копия проекта во временный каталог, в ней ломается ровно одно
правило, прогоняются оба оракула (pytest и сверка с эталоном). Мутант обязан
УМЕРЕТЬ — то есть хотя бы один оракул обязан покраснеть. Выживший мутант
означает, что правило не защищено ничем.

    python tools/mutate.py            прогнать все
    python tools/mutate.py --list     показать, что проверяется
    python tools/mutate.py -k 500     только мутанты, чьё имя содержит «500»

Код возврата 1, если выжил хотя бы один мутант.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COPIED = ("src", "tests", "data", "pytest.ini", "main.py")


@dataclass(frozen=True)
class Mutant:
    """Одна поломка: ровно одно правило, ровно одно место."""

    id: str
    rule: str
    module: str
    old: str
    new: str


# Каждый мутант ломает ровно одно правило спеки. Правило в поле `rule` —
# это то, что обязан защищать хотя бы один тест.
MUTANTS: tuple[Mutant, ...] = (
    Mutant("M01", "расстояние до БС >= 500 м, граница включительно",
           "siting.py", "< MIN_DISTANCE_TO_TOWER_M", "<= MIN_DISTANCE_TO_TOWER_M"),
    Mutant("M02", "покрытие клетки <= 1000 м, граница включительно",
           "siting.py", "<= COVERAGE_RADIUS_M", "< COVERAGE_RADIUS_M"),
    Mutant("M03", "нижняя граница RSRP -140 включительно",
           "config.py", "RSRP_MIN_DBM = -140.0", "RSRP_MIN_DBM = -139.9"),
    Mutant("M04", "верхняя граница RSRP -44 включительно",
           "config.py", "RSRP_MAX_DBM = -44.0", "RSRP_MAX_DBM = -44.1"),
    Mutant("M05", "порог красной зоны: медиана < -105",
           "config.py", "RSRP_YELLOW_MIN = -105.0", "RSRP_YELLOW_MIN = -104.0"),
    Mutant("M06", "порог зелёной зоны: медиана >= -90",
           "config.py", "RSRP_GREEN_MIN = -90.0", "RSRP_GREEN_MIN = -91.0"),
    Mutant("M07", "клетка учитывается при >= 5 замерах",
           "config.py", "MIN_MEASUREMENTS_PER_CELL = 5", "MIN_MEASUREMENTS_PER_CELL = 4"),
    Mutant("M08", "отсечка по минимуму замеров применяется",
           "grid.py", "if len(values) < MIN_MEASUREMENTS_PER_CELL:", "if False:"),
    Mutant("M09", "cos берётся от широты НАЧАЛА СЕТКИ, не от широты точки",
           "geo.py", "math.cos(lat0)", "math.cos(math.radians(lat))"),
    Mutant("M10", "размер клетки 200 м",
           "geo.py", "x / bounds.cell_size_m", "x / (bounds.cell_size_m * 1.01)"),
    Mutant("M11", "тай-брейк худших клеток по cell_id",
           "grid.py", "key=lambda cell: (cell.rsrp_median, cell.cell_id)",
           "key=lambda cell: cell.rsrp_median"),
    Mutant("M12", "дедупликация по четырём нормализованным полям",
           "cleaning.py", "if key in seen:", "if False:"),
    Mutant("M13", "дедупликация сравнивает числа как числа, не как строки",
           "cleaning.py", "key = (lat, lon, rsrp, stamp)",
           "key = tuple(raw_values.values())"),
    Mutant("M14", "пустая физическая строка попадает в dropped_rows",
           "cleaning.py", "if len(row) != len(header):",
           "if row and len(row) != len(header):"),
    Mutant("M15", "границы района включительно",
           "config.py", "self.lat_min <= lat <= self.lat_max",
           "self.lat_min < lat < self.lat_max"),
    Mutant("M16", "отсечка 500 м применяется при выборе пары",
           "pipeline.py", "coverage_sets(eligible, weak, bounds)",
           "coverage_sets(candidates, weak, bounds)"),
    Mutant("M17", "худшие клетки ищутся среди ВСЕХ, а не только красных",
           "pipeline.py", "worst=worst_cells(cells)", "worst=worst_cells(weak)"),
    Mutant("M18", "пара выбирается полным перебором, а не жадно",
           "pipeline.py", "best_ids, covered = best_pair_exhaustive(coverage)",
           "best_ids, covered = best_pair_greedy(coverage)"),
    Mutant("M19", "covered_weak_cells — объединение, а не сумма",
           "siting.py", "len(union) > best_size",
           "sum(len(coverage[s]) for s in combo) > best_size"),
    Mutant("M20", "расстояние считается гаверсинусом, а не евклидом",
           "geo.py", "return 2.0 * earth_radius_m * math.asin(math.sqrt(a))",
           "return earth_radius_m * math.sqrt(dphi ** 2 + dlambda ** 2)"),
)


def run(command: list[str], cwd: Path) -> bool:
    """True, если команда завершилась успешно."""
    finished = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return finished.returncode == 0


def apply_mutation(workspace: Path, mutant: Mutant) -> bool:
    """Применить поломку. False, если искомый текст не найден."""
    target = workspace / "src" / "rsrp_coverage" / mutant.module
    source = target.read_text(encoding="utf-8")
    if mutant.old not in source:
        return False
    target.write_text(source.replace(mutant.old, mutant.new, 1), encoding="utf-8")
    return True


def check_mutant(mutant: Mutant) -> tuple[str, str, str]:
    """Прогнать одного мутанта. Возвращает (вердикт, pytest, эталон)."""
    with tempfile.TemporaryDirectory(prefix=f"mutate-{mutant.id}-") as tmp:
        workspace = Path(tmp) / "project"
        workspace.mkdir()
        for name in COPIED:
            origin = PROJECT_ROOT / name
            if not origin.exists():
                continue
            if origin.is_dir():
                shutil.copytree(origin, workspace / name,
                                ignore=shutil.ignore_patterns("__pycache__", "extracted"))
            else:
                shutil.copy2(origin, workspace / name)

        if not apply_mutation(workspace, mutant):
            return "НЕ ПРИМЕНЁН", "—", "—"

        tests_pass = run([sys.executable, "-m", "pytest", "-q"], workspace)
        reference_pass = run(
            [sys.executable, "-I", "main.py",
             "--data", "data/07_coverage",
             "--out", "out",
             "--expected", "data/07_coverage/expected.json"],
            workspace,
        )

    verdict = "ВЫЖИЛ" if (tests_pass and reference_pass) else "УМЕР"
    return (verdict,
            "passed" if tests_pass else "failed",
            "совпал" if reference_pass else "разошёлся")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Мутационная проверка тестов")
    parser.add_argument("--list", action="store_true",
                        help="показать мутантов и выйти")
    parser.add_argument("-k", dest="pattern", default=None,
                        help="прогнать только мутантов, чей id или правило содержит строку")
    args = parser.parse_args(argv)

    selected = [m for m in MUTANTS
                if not args.pattern
                or args.pattern.lower() in (m.id + m.rule).lower()]

    if args.list:
        for mutant in selected:
            print(f"{mutant.id}  {mutant.module:<12} {mutant.rule}")
        return 0

    if not selected:
        print("Ни один мутант не подошёл под фильтр")
        return 1

    print(f"Мутантов к проверке: {len(selected)}")
    print(f"{'id':<5} {'правило':<58} {'pytest':<8} {'эталон':<10} вердикт")
    print("-" * 100)

    survivors: list[Mutant] = []
    not_applied: list[Mutant] = []
    for mutant in selected:
        verdict, tests, reference = check_mutant(mutant)
        rule = mutant.rule if len(mutant.rule) <= 57 else mutant.rule[:54] + "..."
        print(f"{mutant.id:<5} {rule:<58} {tests:<8} {reference:<10} {verdict}")
        if verdict == "ВЫЖИЛ":
            survivors.append(mutant)
        elif verdict == "НЕ ПРИМЕНЁН":
            not_applied.append(mutant)

    print("-" * 100)
    killed = len(selected) - len(survivors) - len(not_applied)
    print(f"убито {killed} из {len(selected)}")

    if not_applied:
        print("\nНЕ ПРИМЕНИЛИСЬ (код изменился, мутант надо переписать):")
        for mutant in not_applied:
            print(f"  {mutant.id}  {mutant.module}: не найдено «{mutant.old}»")

    if survivors:
        print("\nВЫЖИВШИЕ — правила без защиты:")
        for mutant in survivors:
            print(f"  {mutant.id}  {mutant.rule}")
            print(f"        {mutant.module}: «{mutant.old}» -> «{mutant.new}»")
        return 1

    if not_applied:
        return 1

    print("\nВсе правила защищены тестами.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
