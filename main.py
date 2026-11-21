from __future__ import annotations

import argparse
import csv
from datetime import tzinfo
from itertools import combinations
import json
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory

from coverage_model import (
    COVERAGE_RADIUS_M,
    Cell,
    Measurement,
    aggregate,
    assess_candidates,
    best_pair,
    eligible_coverage_by_radius,
    greedy_pair,
    rank_pairs,
    resilience_front,
)
from data_io import load_grid, load_measurements, load_sites
from map_view import render_map


SCENARIO_RADII_M = (800, 900, COVERAGE_RADIUS_M, 1100, 1200)
RADIUS_RANGES_M = ((800, 1200), (800, 1000), (900, 1100), (1000, 1200))
CRITICAL_THRESHOLDS_DBM = (-110, -115, -120)


def split_periods(
    measurements: list[Measurement],
    local_zone: tzinfo,
) -> dict[str, list[Measurement]]:
    periods: dict[str, list[Measurement]] = {"all": measurements, "day": [], "night": []}
    for measurement in measurements:
        hour = measurement.timestamp.astimezone(local_zone).hour
        period = "day" if 6 <= hour < 18 else "night"
        periods[period].append(measurement)
    return periods


def radius_decisions(
    rankings: dict[int, list[tuple[tuple[str, str], int]]], baseline_pair: tuple[str, str]
) -> dict[str, object]:
    coverage = {radius: dict(ranking) for radius, ranking in rankings.items()}
    leaders = {radius: ranking[0] for radius, ranking in rankings.items()}
    decisions = {}
    for lower, upper in RADIUS_RANGES_M:
        radii = [radius for radius in SCENARIO_RADII_M if lower <= radius <= upper]
        scores = []
        for pair in coverage[COVERAGE_RADIUS_M]:
            gaps = [leaders[radius][1] - coverage[radius][pair] for radius in radii]
            scores.append((max(gaps), sum(gaps), pair))
        max_gap, _, selected_pair = min(scores)

        rows = []
        for radius in radii:
            leader_pair, leader_cells = leaders[radius]
            rows.append({
                "radius_m": radius,
                "leader_ids": list(leader_pair),
                "leader_covered": leader_cells,
                "chosen_covered": coverage[radius][selected_pair],
                "baseline_covered": coverage[radius][baseline_pair],
            })
        decisions[f"{lower}-{upper}"] = {
            "ids": list(selected_pair),
            "max_gap": max_gap,
            "baseline_max_gap": max(row["leader_covered"] - row["baseline_covered"] for row in rows),
            "rows": rows,
        }
    return decisions


def radius_map_data(
    coverage_by_period: dict[str, dict[int, dict[str, set[str]]]], baseline_pair: tuple[str, str]
) -> dict[str, object]:
    reports = {}
    sensitivity = {period: [] for period in coverage_by_period}
    rankings_by_period = {period: {} for period in coverage_by_period}
    baseline_left, baseline_right = baseline_pair

    for radius in SCENARIO_RADII_M:
        pair_rankings = {}
        resilience_options = {}
        for period, coverage_by_radius in coverage_by_period.items():
            coverage = coverage_by_radius[radius]
            ranking = rank_pairs(coverage)
            rankings_by_period[period][radius] = ranking
            best_ids, best_covered = ranking[0]
            baseline_covered = len(coverage[baseline_left] | coverage[baseline_right])
            sensitivity[period].append({
                "radius_m": radius,
                "best_ids": list(best_ids),
                "best_covered": best_covered,
                "baseline_covered": baseline_covered,
            })
            pair_rankings[period] = [
                {"ids": list(pair), "covered_weak_cells": count} for pair, count in ranking
            ]
            resilience_options[period] = resilience_front(coverage, ranking)

        report = {"pair_rankings": pair_rankings, "resilience_options": resilience_options}
        if radius != COVERAGE_RADIUS_M:
            report["coverage_by_period"] = {
                period: {site_id: sorted(cells) for site_id, cells in coverage_by_radius[radius].items()}
                for period, coverage_by_radius in coverage_by_period.items()
            }
        reports[radius] = report

    official_report = reports.pop(COVERAGE_RADIUS_M)
    return {
        **official_report,
        "radius_scenarios": {str(radius): report for radius, report in reports.items()},
        "radius_sensitivity": sensitivity,
        "radius_decisions": {
            period: radius_decisions(rankings, baseline_pair)
            for period, rankings in rankings_by_period.items()
        },
    }


def critical_decisions(
    cells: list[Cell], coverage: dict[str, set[str]], baseline_pair: tuple[str, str]
) -> dict[str, object]:
    pair_coverage = {
        (left, right): coverage[left] | coverage[right]
        for left, right in combinations(sorted(coverage), 2)
    }
    baseline = pair_coverage[baseline_pair]
    decisions = {}
    for threshold in CRITICAL_THRESHOLDS_DBM:
        critical = {cell.id for cell in cells if cell.median <= threshold}
        candidates = [
            (-len(covered & critical), -len(covered), pair)
            for pair, covered in pair_coverage.items()
        ]
        critical_score, weak_score, chosen = min(candidates)
        decisions[str(threshold)] = {
            "ids": list(chosen),
            "critical_total": len(critical),
            "chosen_critical": -critical_score,
            "baseline_critical": len(baseline & critical),
            "chosen_weak": -weak_score,
            "baseline_weak": len(baseline),
        }
    return decisions


def analyze(data_dir: Path) -> tuple[dict[str, object], dict[str, object]]:
    grid, local_zone = load_grid(data_dir / "bounds.json")
    measurements, dropped = load_measurements(data_dir / "measurements.csv", grid, local_zone)
    towers = load_sites(data_dir / "towers.csv")
    sites = load_sites(data_dir / "candidates.csv")
    candidates = assess_candidates(sites, towers, grid)

    periods = split_periods(measurements, local_zone)
    cells_by_period = {name: aggregate(rows, grid) for name, rows in periods.items()}
    coverage_by_period = {
        period: eligible_coverage_by_radius(candidates, cells, grid, SCENARIO_RADII_M)
        for period, cells in cells_by_period.items()
    }
    coverage = {
        period: scenarios[COVERAGE_RADIUS_M] for period, scenarios in coverage_by_period.items()
    }

    overall = coverage["all"]
    best = best_pair(overall)
    greedy = greedy_pair(overall)
    covered_ids = overall[best[0]] | overall[best[1]]
    worst = sorted(cells_by_period["all"], key=lambda cell: (cell.median, cell.id))[:3]
    result = {
        "worst_cells": [{"cell_id": cell.id, "rsrp_median": cell.median} for cell in worst],
        "best_candidates": list(best),
        "dropped_rows": dropped,
        "covered_weak_cells": len(covered_ids),
    }

    layers = {}
    summaries = {}
    for name, cells in cells_by_period.items():
        layers[name] = [
            {
                "id": cell.id,
                "bounds": grid.cell_bounds(cell.ix, cell.iy),
                "median": cell.median,
                "count": cell.count,
                "state": cell.state,
            }
            for cell in cells
        ]
        summaries[name] = {
            "measurements": len(periods[name]),
            "cells": len(cells),
            "weak_cells": sum(cell.state == "weak" for cell in cells),
        }

    map_data = {
        "bounds": grid.bounds,
        "layers": layers,
        "summaries": summaries,
        "towers": [{"id": tower.id, "lat": tower.lat, "lon": tower.lon} for tower in towers],
        "candidates": [
            {
                "id": candidate.site.id,
                "lat": candidate.site.lat,
                "lon": candidate.site.lon,
                "nearest_m": candidate.nearest_m,
                "eligible": candidate.eligible,
                "covered_by_period": {
                    name: sorted(site_coverage.get(candidate.site.id, ()))
                    for name, site_coverage in coverage.items()
                },
            }
            for candidate in candidates
        ],
        "strategies": {
            name: {
                "ids": list(pair),
                "covered_by_period": {
                    period: sorted(site_coverage[pair[0]] | site_coverage[pair[1]])
                    for period, site_coverage in coverage.items()
                },
            }
            for name, pair in (("optimal", best), ("greedy", greedy))
        },
        **radius_map_data(coverage_by_period, best),
        "critical_decisions": {
            period: critical_decisions(cells, coverage[period], best)
            for period, cells in cells_by_period.items()
        },
        "result": result,
    }
    return result, map_data


def write_outputs(out_dir: Path, result: dict[str, object], page: str) -> None:
    outputs = {
        "map.html": page,
        "result.json": json.dumps(result, ensure_ascii=False, indent=2) + "\n",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".coverage-", dir=out_dir) as directory:
        staging = Path(directory)
        for name, contents in outputs.items():
            target = out_dir / name
            if target.is_dir():
                raise IsADirectoryError(f"Вместо выходного файла существует папка {target}")
            (staging / name).write_text(contents, encoding="utf-8")

        result_path = out_dir / "result.json"
        previous_result = staging / "previous-result.json"
        had_result = result_path.exists() or result_path.is_symlink()
        if had_result:
            copy2(result_path, previous_result, follow_symlinks=False)

        (staging / "result.json").replace(result_path)
        try:
            (staging / "map.html").replace(out_dir / "map.html")
        except (OSError, KeyboardInterrupt):
            if had_result:
                previous_result.replace(result_path)
            else:
                result_path.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Построить карту покрытия и выбрать две площадки.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="папка с CSV и bounds.json")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="папка для map.html и result.json")
    args = parser.parse_args()

    try:
        result, map_data = analyze(args.data_dir)
        write_outputs(args.out_dir, result, render_map(map_data))
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Ошибка: {error}\n")

    print(f"Отброшено строк: {result['dropped_rows']}")
    print(f"Карта: {args.out_dir / 'map.html'}")
    print(f"Результат: {args.out_dir / 'result.json'}")


if __name__ == "__main__":
    main()
