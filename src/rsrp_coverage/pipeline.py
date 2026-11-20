"""Сборка полного прогона: очистка -> сетка -> худшие клетки -> выбор пары."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .cleaning import CleaningReport, load_measurements
from .config import Bounds
from .grid import Cell, build_grid, count_cells_with_measurements, weak_cells, worst_cells
from .siting import (Site, best_pair_exhaustive, best_pair_greedy, coverage_sets,
                     eligible_candidates, load_sites)


@dataclass
class Result:
    bounds: Bounds
    report: CleaningReport
    cells: list[Cell]
    cells_with_measurements: int
    worst: list[Cell]
    weak: list[Cell]
    towers: list[Site]
    candidates: list[Site]
    eligible: list[Site]
    best_candidates: list[str]
    covered_weak_cells: int
    greedy_candidates: list[str]
    greedy_covered: int

    def to_result_json(self) -> dict:
        """Ровно тот формат, который сверяет жюри."""
        return {
            "worst_cells": [
                {"cell_id": cell.cell_id, "rsrp_median": cell.rsrp_median}
                for cell in self.worst
            ],
            "best_candidates": self.best_candidates,
            "dropped_rows": self.report.dropped_rows,
            "covered_weak_cells": self.covered_weak_cells,
        }


def run(data_dir: str | Path) -> Result:
    """Полный прогон по каталогу с measurements.csv, towers.csv, candidates.csv, bounds.json."""
    base = Path(data_dir)
    bounds = Bounds.from_json(base / "bounds.json")

    measurements, report = load_measurements(base / "measurements.csv", bounds)
    cells = build_grid(measurements, bounds)
    with_measurements = count_cells_with_measurements(measurements, bounds)
    weak = weak_cells(cells)

    towers = load_sites(base / "towers.csv")
    candidates = load_sites(base / "candidates.csv")
    eligible = eligible_candidates(candidates, towers, bounds)

    coverage = coverage_sets(eligible, weak, bounds)
    best_ids, covered = best_pair_exhaustive(coverage)
    greedy_ids, greedy_covered = best_pair_greedy(coverage)

    return Result(
        bounds=bounds,
        report=report,
        cells=cells,
        cells_with_measurements=with_measurements,
        worst=worst_cells(cells),
        weak=weak,
        towers=towers,
        candidates=candidates,
        eligible=eligible,
        best_candidates=best_ids,
        covered_weak_cells=covered,
        greedy_candidates=greedy_ids,
        greedy_covered=greedy_covered,
    )
