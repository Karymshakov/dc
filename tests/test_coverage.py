import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from coverage_model import (
    Candidate,
    Cell,
    Grid,
    Measurement,
    Site,
    aggregate,
    best_pair,
    eligible_coverage,
    eligible_coverage_by_radius,
    greedy_pair,
    haversine,
    rank_pairs,
)
from data_io import load_grid, load_measurements, load_sites
from main import analyze, write_outputs


ROOT = Path(__file__).resolve().parents[1]
RADIUS = 6_371_000
GRID = Grid(0, 0.05, 0, 0.05, 0, 0, 200, RADIUS)
BISHKEK = timezone(timedelta(hours=6))


def degrees_for_meters(meters):
    return math.degrees(meters / RADIUS)


class CoverageTests(unittest.TestCase):
    def test_cleaning_counts_invalid_rows_and_normalized_duplicates(self):
        rows = [
            'lat,lon,rsrp_dbm,timestamp',
            '0,0,-140,2026-11-20T08:00:00+06:00',
            '0.00,0,-140.0,2026-11-20T08:00:00+06:00',
            '0,0,-140,2026-11-20T02:00:00Z',
            '0,0,-140,2026-11-20T08:00:00',
            '0,0,-140,2026-11-20T08:00:01+06:00',
            '0,0,-44,2026-11-20T08:00:00+06:00',
            '"0,0,-100,2026-11-20T08:00:00+06:00',
            '0.05,0.05,-44,2026-11-20T08:00:00+06:00',
            '',
            '0,0,-100',
            '0,0,-100,2026-11-20T08:00:00+06:00,extra',
            'NaN,0,-100,2026-11-20T08:00:00+06:00',
            '0,inf,-100,2026-11-20T08:00:00+06:00',
            '0,0,-1_00,2026-11-20T08:00:00+06:00',
            '0,0,-140.1,2026-11-20T08:00:00+06:00',
            '0,0,-43.9,2026-11-20T08:00:00+06:00',
            '0.05001,0,-100,2026-11-20T08:00:00+06:00',
            '0,0,-100,2026-13-20T08:00:00+06:00',
            '0,0,-100,2026-11-20',
            '0,0,-100,2026-11-20T08:00:00+00:99',
            '0,0,-100,2026-11-20T08:00:00-09:99',
            '0,0,-100,2026-11-20T08:00:00+05:15:99',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'measurements.csv'
            path.write_text('\n'.join(rows) + '\n', encoding='utf-8')
            measurements, dropped = load_measurements(path, GRID, BISHKEK)

        self.assertEqual(dropped, 18)
        self.assertEqual(len(measurements), 4)

    def test_grid_loader_reports_invalid_scale(self):
        bounds = json.loads((ROOT / 'data' / 'bounds.json').read_text(encoding='utf-8'))
        bounds['cell_size_m'] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bounds.json'
            path.write_text(json.dumps(bounds), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, r'bounds\.json:.*cell_size_m'):
                load_grid(path)

    def test_site_loader_reports_duplicate_id_and_row_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sites.csv'
            path.write_text('id,lat,lon\nC01,0,0\nC01,0,1\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, r'sites\.csv:3:.*unique'):
                load_sites(path)

    def test_grid_uses_longitude_for_x_and_latitude_for_y(self):
        self.assertTrue(GRID.contains(0, 0))
        self.assertTrue(GRID.contains(0.05, 0.05))
        self.assertFalse(GRID.contains(0, 0.05001))
        self.assertEqual(GRID.index(degrees_for_meters(250), degrees_for_meters(650)), (3, 1))

    def test_aggregation_requires_five_measurements_and_classifies_median(self):
        timestamp = datetime(2026, 11, 20, 12, tzinfo=BISHKEK)
        measurements = [
            Measurement(0, degrees_for_meters(east), value, timestamp)
            for east, readings in (
                (100, [-90] * 5),
                (300, [-116, -111, -105, -95, -90]),
                (500, [-106] * 5),
                (700, [-130] * 4),
            )
            for value in readings
        ]

        cells = {cell.id: (cell.median, cell.state) for cell in aggregate(measurements, GRID)}

        self.assertEqual(cells, {
            'X000_Y000': (-90, 'strong'),
            'X001_Y000': (-105, 'fair'),
            'X002_Y000': (-106, 'weak'),
        })

    def test_only_eligible_candidates_cover_weak_cells_by_center(self):
        center = degrees_for_meters(100)
        weak = Cell(0, 0, -110, 5)
        fair = Cell(0, 1, -95, 5)
        at_limit = Site('C01', center + degrees_for_meters(1000), center)
        outside = Site('C02', center + degrees_for_meters(1001), center)
        too_close = Site('C03', center, center)
        candidates = [Candidate(at_limit, 500), Candidate(outside), Candidate(too_close, 499)]

        self.assertAlmostEqual(haversine((center, center), (at_limit.lat, at_limit.lon), RADIUS), 1000)
        self.assertTrue(candidates[0].eligible)
        self.assertFalse(candidates[2].eligible)
        self.assertEqual(eligible_coverage(candidates, [weak, fair], GRID), {
            'C01': {weak.id},
            'C02': set(),
        })

    def test_cell_at_exact_computed_radius_is_covered(self):
        candidate = Candidate(Site('C01', 0, 0), 500)
        weak = Cell(0, 0, -110, 5)
        with patch('coverage_model.haversine', return_value=1000) as distance:
            coverage = eligible_coverage_by_radius([candidate], [weak], GRID, (900, 1000, 1100))
        distance.assert_called_once()
        self.assertEqual(coverage, {
            900: {'C01': set()},
            1000: {'C01': {weak.id}},
            1100: {'C01': {weak.id}},
        })

    def test_pair_maximizes_unique_cells_and_breaks_ties_by_id(self):
        coverage = {
            'D': {'4', '5', '6', '9', '10'},
            'C': {'4', '5', '6', '9', '10'},
            'B': {'1', '2', '3', '7', '8'},
            'A': {'1', '2', '3', '4', '5', '6'},
        }

        self.assertEqual(best_pair(coverage), ('B', 'C'))
        self.assertEqual(greedy_pair(coverage), ('A', 'B'))
        self.assertEqual(rank_pairs(coverage), [
            (('B', 'C'), 10), (('B', 'D'), 10),
            (('A', 'B'), 8), (('A', 'C'), 8), (('A', 'D'), 8),
            (('C', 'D'), 5),
        ])

    def test_pair_rankings_match_candidate_coverage_for_each_period(self):
        result, map_data = analyze(ROOT / 'data')
        candidates = {row['id']: row['covered_by_period'] for row in map_data['candidates'] if row['eligible']}
        expected_pairs = set(combinations(sorted(candidates), 2))
        self.assertEqual(set(map_data['pair_rankings']), {'all', 'day', 'night'})
        for period, ranking in map_data['pair_rankings'].items():
            self.assertEqual(len(ranking), len(expected_pairs))
            self.assertEqual({tuple(item['ids']) for item in ranking}, expected_pairs)
            self.assertEqual(ranking, sorted(ranking, key=lambda item: (-item['covered_weak_cells'], item['ids'])))
            for item in ranking:
                left, right = item['ids']
                union = set(candidates[left][period]) | set(candidates[right][period])
                self.assertEqual(item['covered_weak_cells'], len(union))
        self.assertEqual(map_data['pair_rankings']['all'][0], {
            'ids': result['best_candidates'], 'covered_weak_cells': result['covered_weak_cells'],
        })

    def test_strategy_layers_match_candidate_coverage_by_period(self):
        result, map_data = analyze(ROOT / 'data')
        candidates = {item['id']: item for item in map_data['candidates']}

        for strategy in map_data['strategies'].values():
            left, right = strategy['ids']
            for period, cell_ids in strategy['covered_by_period'].items():
                left_cells = candidates[left]['covered_by_period'][period]
                right_cells = candidates[right]['covered_by_period'][period]
                self.assertEqual(cell_ids, sorted(set(left_cells) | set(right_cells)))

        optimal = map_data['strategies']['optimal']
        self.assertEqual(optimal['ids'], result['best_candidates'])
        self.assertEqual(len(optimal['covered_by_period']['all']), result['covered_weak_cells'])
        for candidate in candidates.values():
            if not candidate['eligible']:
                self.assertEqual(candidate['covered_by_period'], {'all': [], 'day': [], 'night': []})

    def test_resilience_front_contains_only_undominated_distinct_scores(self):
        _, map_data = analyze(ROOT / 'data')
        single = {item['id']: item['covered_by_period'] for item in map_data['candidates']}
        options = map_data['resilience_options']
        self.assertEqual(set(options), {'all', 'day', 'night'})
        self.assertEqual([
            (item['ids'], item['covered_weak_cells'], item['worst_single_site_cells'])
            for item in options['all']
        ], [(['C09', 'C14'], 111, 52), (['C09', 'C16'], 105, 61), (['C08', 'C16'], 98, 66)])

        for period, ranking in map_data['pair_rankings'].items():
            counts = {site: len(cells[period]) for site, cells in single.items()}
            scores = {}
            for item in ranking:
                pair = item['ids']
                score = item['covered_weak_cells'], min(counts[site] for site in pair)
                scores[score] = min(pair, scores.get(score, pair))

            expected = []
            for total, remaining in sorted(scores, reverse=True):
                if any(a >= total and b >= remaining and (a, b) != (total, remaining) for a, b in scores):
                    continue
                pair = scores[total, remaining]
                expected.append({
                    'ids': pair,
                    'covered_weak_cells': total,
                    'worst_single_site_cells': remaining,
                    'single_site_cells': {site: counts[site] for site in pair},
                })
            self.assertEqual(options[period], expected)

    def test_radius_scenarios_are_nested_and_preserve_the_official_coverage(self):
        result, map_data = analyze(ROOT / 'data')
        scenarios = map_data['radius_scenarios']
        sensitivity = map_data['radius_sensitivity']
        self.assertEqual(set(scenarios), {'800', '900', '1100', '1200'})
        self.assertEqual([
            (row['radius_m'], row['best_covered'], row['baseline_covered']) for row in sensitivity['all']
        ], [(800, 80, 75), (900, 101, 99), (1000, 111, 111), (1100, 127, 127), (1200, 138, 138)])
        baseline_left, baseline_right = result['best_candidates']

        for period, rows in sensitivity.items():
            official = {
                site['id']: site['covered_by_period'][period] for site in map_data['candidates'] if site['eligible']
            }
            previous = {site_id: set() for site_id in official}
            for row in rows:
                radius = row['radius_m']
                scenario = scenarios.get(str(radius), map_data)
                covers = official if radius == 1000 else scenario['coverage_by_period'][period]
                current = {site_id: set(cells) for site_id, cells in covers.items()}
                self.assertEqual(current.keys(), previous.keys())
                for site_id, cells in previous.items():
                    self.assertLessEqual(cells, current[site_id])
                first = scenario['pair_rankings'][period][0]
                self.assertEqual((row['best_ids'], row['best_covered']), (first['ids'], first['covered_weak_cells']))
                self.assertEqual(row['baseline_covered'], len(current[baseline_left] | current[baseline_right]))
                if radius == 1000 and period == 'all':
                    self.assertEqual(row['best_ids'], result['best_candidates'])
                    self.assertEqual(row['best_covered'], result['covered_weak_cells'])
                previous = current

    def test_radius_decisions_minimize_the_largest_gap_to_a_scenario_leader(self):
        result, map_data = analyze(ROOT / 'data')
        baseline = tuple(result['best_candidates'])
        for period, decisions in map_data['radius_decisions'].items():
            for bounds, decision in decisions.items():
                lower, upper = map(int, bounds.split('-'))
                rows = decision['rows']
                radii = list(range(lower, upper + 1, 100))
                self.assertEqual([row['radius_m'] for row in rows], radii)

                gaps = {}
                selected = tuple(decision['ids'])
                for radius, row in zip(radii, rows):
                    report = map_data['radius_scenarios'].get(str(radius), map_data)
                    ranking = report['pair_rankings'][period]
                    self.assertEqual(row['leader_ids'], ranking[0]['ids'])
                    self.assertEqual(row['leader_covered'], ranking[0]['covered_weak_cells'])
                    for option in ranking:
                        pair = tuple(option['ids'])
                        cells = option['covered_weak_cells']
                        gaps.setdefault(pair, []).append(row['leader_covered'] - cells)
                        if pair == selected:
                            self.assertEqual(row['chosen_covered'], cells)
                        if pair == baseline:
                            self.assertEqual(row['baseline_covered'], cells)

                self.assertEqual((decision['max_gap'], sum(gaps[selected]), selected),
                                 min((max(items), sum(items), pair) for pair, items in gaps.items()))
                self.assertEqual(decision['baseline_max_gap'], max(gaps[baseline]))

        short_range = map_data['radius_decisions']['all']['800-1000']
        self.assertEqual((short_range['ids'], short_range['max_gap'], short_range['baseline_max_gap']),
                         (['C16', 'C17'], 3, 5))

    def test_critical_decisions_prioritize_the_lowest_rsrp_cells(self):
        result, map_data = analyze(ROOT / 'data')
        baseline = tuple(result['best_candidates'])
        eligible = [row for row in map_data['candidates'] if row['eligible']]
        for period, decisions in map_data['critical_decisions'].items():
            coverage = {row['id']: set(row['covered_by_period'][period]) for row in eligible}
            for threshold, decision in decisions.items():
                critical = {row['id'] for row in map_data['layers'][period] if row['median'] <= int(threshold)}
                baseline_cells = coverage[baseline[0]] | coverage[baseline[1]]
                scores = {}
                for left, right in combinations(sorted(coverage), 2):
                    covered = coverage[left] | coverage[right]
                    scores[left, right] = (len(covered & critical), len(covered))

                best_score = max(scores.values())
                chosen = min(pair for pair, score in scores.items() if score == best_score)
                self.assertEqual(decision, {
                    'ids': list(chosen), 'critical_total': len(critical),
                    'chosen_critical': best_score[0], 'chosen_weak': best_score[1],
                    'baseline_critical': len(baseline_cells & critical), 'baseline_weak': len(baseline_cells),
                })

        decision = map_data['critical_decisions']['all']['-115']
        self.assertEqual((decision['ids'], decision['chosen_critical'], decision['baseline_critical']),
                         (['C16', 'C17'], 13, 9))

    def test_cli_matches_open_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            run = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', 'main.py',
                 '--data-dir', 'data', '--out-dir', str(output)],
                cwd=ROOT,
                capture_output=True,
                encoding='utf-8',
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            expected = json.loads((ROOT / 'data' / 'expected.json').read_text(encoding='utf-8'))
            self.assertEqual(result, expected)
            page = (output / 'map.html').read_text(encoding='utf-8')
            self.assertNotRegex(page, r'@@[A-Z_]+@@')
            embedded = re.search(r'<script\b[^>]*\bid="buildings-data"[^>]*>(.*?)</script>', page, re.S)
            self.assertIsNotNone(embedded)
            source = ROOT / 'web' / 'buildings.json'
            expected_buildings = {'bounds': [], 'source': '', 'buildings': []}
            if source.is_file():
                expected_buildings = json.loads(source.read_text(encoding='utf-8'))
            self.assertEqual(json.loads(embedded[1]), expected_buildings)
            radio = re.search(r'<script\b[^>]*\bid="radio-data"[^>]*>(.*?)</script>', page, re.S)
            self.assertIsNotNone(radio)
            atlas = json.loads(radio[1])
            self.assertEqual(atlas["example"], ["C15", "X011_Y015"])
            route = atlas["routes"]["C15"]["X011_Y015"]
            self.assertEqual(route["losses"]["1800"][atlas["heights"].index(25)], [0, 37.1, 37.1])
            self.assertEqual(route["losses"]["1800"][atlas["heights"].index(40)], [0, 10.2, 10.2])
            for cell in ("X013_Y010", "X012_Y008"):
                route = atlas["routes"]["C13"][cell]
                self.assertEqual(route["status"], "endpoint")
                self.assertTrue(route["siteIndoors"])
                self.assertNotIn("losses", route)
                self.assertTrue(any(id_ == "w167714192" and start == 0 for id_, start, *_ in route["crossings"]))
            all_periods = atlas["pairs"]["all"]
            self.assertEqual(all_periods["official"], {"inRadius": 111, "assessed": 0})
            audit = all_periods["dataAudit"]
            self.assertEqual(len(audit), 111)
            first_reasons = [paths[0][1] for paths in audit.values()]
            self.assertEqual(
                {reason: first_reasons.count(reason) for reason in set(first_reasons)},
                {"heights": 51, "site": 49, "receiver": 9, "coincident": 2},
            )
            self.assertEqual(audit["X009_Y012"], [["C09", "heights"], ["C14", "site"]])
            self.assertEqual(audit["X012_Y013"], [["C14", "site"]])
            comparison = all_periods["scenarios"]["1800"][atlas["heights"].index(25)]
            self.assertEqual(comparison["reference"], ["C15", "C19"])
            self.assertEqual(comparison["alternative"], ["C10", "C15"])
            self.assertEqual((comparison["assessed"], comparison["commonRadius"]), (8, 66))
            self.assertEqual(comparison["terrain"], [8, 8])
            self.assertEqual(comparison["roofs"], [2, 3])
            self.assertEqual(comparison["focus"], {
                "cell": "X009_Y001", "reference": {"site": "C19", "terrain": 0, "withRoofs": 45.6},
                "alternative": {"site": "C10", "terrain": 0, "withRoofs": 0},
            })
            self.assertIsNone(comparison["regional"])
            rooftop = atlas["roofStudies"]["X012_Y013"]
            self.assertEqual(rooftop["cell"], "X012_Y013")
            self.assertEqual(rooftop["minHeight"], 10)
            i = atlas["heights"].index(25)
            self.assertEqual(rooftop["sites"]["C14"]["losses"]["1800"][i], [0, 39.9, 39.9])
            self.assertEqual(rooftop["sites"]["C15"]["losses"]["1800"][i], [0, 0, 0])
            self.assertEqual(rooftop["spread"]["C14"]["1800"][i], [35.3, 44.6])
            self.assertEqual(atlas["routes"]["C14"]["X012_Y013"]["status"], "endpoint")
            second = atlas["roofStudies"]["X010_Y015"]
            self.assertEqual(second["variants"], 24)
            self.assertEqual(second["sites"]["C14"]["losses"]["1800"][i], [0, 38.4, 38.4])
            self.assertEqual(second["sites"]["C15"]["losses"]["1800"][i], [0, 0, 0])
            self.assertEqual(second["spread"]["C14"]["1800"][i], [35.3, 43.8])
            self.assertEqual(second["spread"]["C15"]["1800"][i], [0, 43.4])
            self.assertEqual(atlas["routes"]["C14"]["X010_Y015"]["status"], "endpoint")

    def test_map_write_failure_restores_previous_result(self):
        real_replace = Path.replace

        def locked_map(source, target):
            if source.name == 'map.html':
                raise PermissionError('map is locked')
            return real_replace(source, target)

        for had_result in (False, True):
            with self.subTest(had_result=had_result), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                result = output / 'result.json'
                page = output / 'map.html'
                page.write_text('previous map', encoding='utf-8')
                if had_result:
                    result.write_text('previous result', encoding='utf-8')

                with patch.object(Path, 'replace', locked_map):
                    with self.assertRaisesRegex(PermissionError, 'map is locked'):
                        write_outputs(output, {'version': 'new'}, 'new map')

                self.assertEqual(page.read_text(encoding='utf-8'), 'previous map')
                if had_result:
                    self.assertEqual(result.read_text(encoding='utf-8'), 'previous result')
                else:
                    self.assertFalse(result.exists())


if __name__ == '__main__':
    unittest.main()
