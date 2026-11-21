import math
import unittest

from building_geometry import BuildingIndex, METRES_PER_DEGREE
from radio_pair_comparison import compare_pairs
from radio_profile import (
    HEIGHTS_M,
    Terrain,
    _bullington_parameter,
    _effective_profile,
    build_route,
    diffraction_loss_db,
)


def point(x, y=0):
    return y / METRES_PER_DEGREE, x / METRES_PER_DEGREE


def rectangle(left, bottom, right, top):
    return [point(x, y)[::-1] for x, y in (
        (left, bottom), (right, bottom), (right, top), (left, top), (left, bottom)
    )]


def building(identifier, polygons, height, base=0):
    return dict(id=identifier, height=height, base=base, polygons=polygons)


def make_index(*buildings):
    return BuildingIndex({
        "bounds": [-500 / METRES_PER_DEGREE, -500 / METRES_PER_DEGREE,
                   500 / METRES_PER_DEGREE, 500 / METRES_PER_DEGREE],
        "buildings": buildings,
    })


class RadioProfileTests(unittest.TestCase):
    def setUp(self):
        self.ground = Terrain(-1, 1, 1, 1, [[7500, 7500], [7500, 7500]])

    def test_dem_interpolates_pixel_centers_and_rejects_extrapolation(self):
        terrain = Terrain(74, 43, 0.01, 0.01, [[7000, 7100], [7200, 7300]])
        self.assertAlmostEqual(terrain.elevation((42.995, 74.005)), 700)
        self.assertAlmostEqual(terrain.elevation((42.99, 74.01)), 715)
        self.assertEqual(terrain.elevation((42.98, 74.02)), 730)
        self.assertIsNone(terrain.elevation((42.99, 74.021)))

    def test_courtyard_is_a_gap_and_duplicate_polygon_intervals_are_merged(self):
        outer, hole = rectangle(20, -10, 80, 10), rectangle(45, -5, 55, 5)
        scene = make_index(building("A", [[outer, hole], [rectangle(25, -10, 40, 10)]], 40))
        spans = scene.crossings(point(0), point(100), 100)
        self.assertEqual([(round(left), round(right)) for left, right, _ in spans], [(20, 45), (55, 80)])

    def test_boundary_and_vertex_contacts_have_no_footprint_interior(self):
        box = building("wall", [[rectangle(10, 0, 45, 10)]], 50)
        tip = [point(x, y)[::-1] for x, y in [(50, 0), (65, 12), (35, 8), (50, 0)]]
        scene = make_index(box, building("vertex", [[tip]], 50))
        self.assertEqual(scene.crossings(point(0), point(100), 100), [])

    def test_diagonal_routes_reverse_the_footprint_distances(self):
        scene = make_index(building("A", [[rectangle(30, 30, 60, 60)]], 25))
        total = 100 * math.sqrt(2)
        left, right, _ = scene.crossings(point(0), point(100, 100), total)[0]
        back_left, back_right, _ = scene.crossings(point(100, 100), point(0), total)[0]
        self.assertAlmostEqual(left, 30 * math.sqrt(2))
        self.assertAlmostEqual(right, 60 * math.sqrt(2))
        self.assertAlmostEqual(left + back_right, total)
        self.assertAlmostEqual(right + back_left, total)

    def test_single_obstacle_agrees_with_p526_in_kilometres(self):
        # Calculate the reference independently with P.526 units (GHz, km, metres).
        for height in (5, 10, 15, 27):
            for freq in (900, 1800, 2600):
                with self.subTest(roof=height, frequency=freq):
                    profile = _effective_profile((0, 180, 400), (0, height, 0), 400)
                    actual = diffraction_loss_db(_bullington_parameter(profile, 400, 10, 10), 400, freq)
                    corrected_height = height - 10 + 500 * (0.18 * 0.22) / (6371 * 4 / 3)
                    wavelength = 0.299792458 / (freq / 1000)
                    nu = corrected_height * math.sqrt(0.002 * 0.4 / (wavelength * 0.18 * 0.22))
                    z = nu - 0.1
                    j = 0 if nu <= -0.78 else 6.9 + 20 * math.log10(math.sqrt(z * z + 1) + z)
                    expected = j + (1 - math.exp(-j / 6)) * (10 + 0.02 * 0.4)
                    self.assertAlmostEqual(actual, expected, places=10)

    def test_two_obstacles_use_intersection_of_grazing_rays(self):
        sample = _effective_profile([0, 100, 300, 400], [0, 23, 23, 0], 400)
        a = _bullington_parameter(sample, 400, 10, 10)
        midpoint = sample[0][1]
        expected = (2 * (midpoint - 10)) * math.sqrt(2 * 400 / (200 * 200))
        self.assertAlmostEqual(a, expected)
        self.assertGreater(diffraction_loss_db(a, 400, 1800), 10)

    def test_roof_contribution_and_obstruction_change_when_the_antenna_rises(self):
        scene = make_index(building("A", [[rectangle(43, -8, 60, 8)]], 21))
        route = build_route(self.ground, scene, point(0), point(125))
        low, high = (HEIGHTS_M.index(h) for h in (25, 40))
        self.assertEqual(route["status"], "modeled")
        self.assertGreater(route["losses"][1800][low][2], route["losses"][1800][high][2])
        mask = route["crossings"][0][-1]
        self.assertTrue(mask & (1 << low))
        self.assertFalse(mask & (1 << high))
        self.assertGreater(route["losses"][2600][low][2], route["losses"][900][low][2])

    def test_no_roof_keeps_losses_identical(self):
        route = build_route(self.ground, make_index(), point(0), point(500))
        self.assertEqual(route["status"], "modeled")
        for trials in route["losses"].values():
            self.assertTrue(all(land == all_loss and roofs == 0 for land, all_loss, roofs in trials))

    def test_endpoint_routes_withhold_db_even_when_the_building_has_no_height(self):
        scene = make_index(building("A", [[rectangle(40, -10, 120, 10)]], None))
        for origin, target in ((point(0), point(100)), (point(100), point(0))):
            route = build_route(self.ground, scene, origin, target)
            self.assertEqual(route["status"], "endpoint")
            self.assertNotIn("losses", route)

    def test_rooftop_scenario_requires_a_known_origin_roof_and_height_above_it(self):
        scene = make_index(building("A", [[rectangle(-10, -10, 10, 10)]], 7))
        origin, target = point(0), point(100)
        self.assertEqual(build_route(self.ground, scene, origin, target)["status"], "endpoint")
        self.assertEqual(build_route(self.ground, scene, origin, target, rooftop="B")["status"], "endpoint")
        trial = build_route(self.ground, scene, origin, target, rooftop="A")
        self.assertEqual(trial["status"], "modeled")
        self.assertIsNone(trial["losses"][1800][HEIGHTS_M.index(5)])
        self.assertIsNotNone(trial["losses"][1800][HEIGHTS_M.index(10)])
        self.assertEqual(build_route(self.ground, scene, target, origin, rooftop="A")["status"], "endpoint")

    def test_coincident_and_outside_are_explicit(self):
        scene = make_index()
        self.assertEqual(build_route(self.ground, scene, point(0), point(0))["status"], "coincident")
        self.assertEqual(build_route(self.ground, scene, point(0), point(600))["status"], "outside")

    def test_unmodeled_roofs_count_once_per_object(self):
        scene = make_index(
            building("unknown", [[rectangle(10, -2, 20, 2)], [rectangle(40, -2, 45, 2)]], None),
            building("raised", [[rectangle(70, -2, 75, 2)]], 25, base=5),
        )
        route = build_route(self.ground, scene, point(0), point(100))
        self.assertEqual(route["partial"], 2)
        self.assertEqual(len(route["crossings"]), 3)
        self.assertEqual(route["losses"][1800][0][2], 0)

    @staticmethod
    def compare_fixture(*, partial=0, reference_ground=0):
        coverage = {site: {"covered_by_period": {"all": ids}} for site, ids in {
            "A": ["x", "y"], "B": ["y"], "C": ["x", "y"],
        }.items()}

        def modeled(ground, combined, partial_count=0):
            return {"status": "modeled", "partial": partial_count,
                    "losses": {1800: [[ground, combined, combined - ground]]}}

        routes = {
            "A": {"x": modeled(reference_ground, 35, partial), "y": modeled(0, 12)},
            "B": {"y": modeled(0, 0)},
            "C": {"x": modeled(0, 10), "y": modeled(0, 0)},
        }
        report = compare_pairs(["all"], coverage, routes, ["A", "B"], [1800], [25], 10)
        return report["all"]["official"], report["all"]["scenarios"][1800][0]

    def test_pair_comparison_controls_radii_and_deduplicates_cells(self):
        official, example = self.compare_fixture()
        self.assertEqual(official, {"inRadius": 2, "assessed": 2})
        self.assertEqual(example["reference"], ["A", "B"])
        self.assertEqual(example["assessed"], 2)
        self.assertEqual(example["terrain"], [2, 2])
        self.assertEqual(example["roofs"], [1, 2])
        self.assertEqual(example["gained"], ["x"])
        self.assertEqual(example["focus"]["reference"]["site"], "A")
        self.assertIsNotNone(example["regional"])

    def test_missing_heights_are_not_a_loss_or_a_gain_in_pair_comparison(self):
        official, example = self.compare_fixture(partial=1)
        self.assertEqual(official, {"inRadius": 2, "assessed": 1})
        self.assertIsNone(example)

    def test_pair_already_ahead_on_terrain_does_not_get_credit_for_a_roof_switch(self):
        _, example = self.compare_fixture(reference_ground=12)
        self.assertIsNone(example)
