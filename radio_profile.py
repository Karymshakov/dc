"""Build outdoor diffraction profiles for the standalone map."""

from dataclasses import dataclass
from math import asinh, ceil, expm1, floor, hypot, log, sqrt

from building_geometry import BuildingIndex, EPSILON_M, Point
from radio_pair_comparison import compare_pairs

HEIGHTS_M = tuple(range(5, 81, 5))
FREQUENCIES_MHZ = (900, 1800, 2600)
RECEIVER_M = 1.5
SCREENING_LIMIT_DB = 10
EFFECTIVE_EARTH_RADIUS_M = 6_371_000 * 4 / 3
LIGHT_SPEED_M_S = 299_792_458


@dataclass(frozen=True)
class Terrain:
    west: float
    north: float
    step_lon: float
    step_lat: float
    height_dm: list[list[int]]

    @classmethod
    def from_data(cls, data):
        return cls(data["west"], data["north"], data["stepLon"], data["stepLat"], data["heightDm"])

    def elevation(self, point: Point) -> float | None:
        lat, lon = point
        x, y = (lon - self.west) / self.step_lon, (self.north - lat) / self.step_lat
        rows, cols = len(self.height_dm), len(self.height_dm[0])
        if not (-1e-6 <= x <= cols + 1e-6 and -1e-6 <= y <= rows + 1e-6):
            return None
        # The raster origin is the corner of a pixel. Heights belong to its center.
        x = min(max(x - 0.5, 0), cols - 1)
        y = min(max(y - 0.5, 0), rows - 1)
        i, j = floor(x), floor(y)
        east, south = min(i + 1, cols - 1), min(j + 1, rows - 1)
        u, v = x - i, y - j
        top = self.height_dm[j][i] * (1 - u) + self.height_dm[j][east] * u
        bottom = self.height_dm[south][i] * (1 - u) + self.height_dm[south][east] * u
        return (top * (1 - v) + bottom * v) / 10


def _bullington_parameter(profile, distance: float, tx: float, rx: float) -> float:
    """ITU-R P.526-16 §4.5.1, equations 49–56, with metres throughout.

    The profile includes effective Earth curvature. The result takes the
    wavelength factor separately so the same profile supports several bands.
    """
    direct = (rx - tx) / distance
    tx_slope = rx_slope = best = float("-inf")
    for along, elevation, factor in profile:
        relative = elevation - tx
        tx_slope = max(tx_slope, relative / along)
        rx_slope = max(rx_slope, (elevation - rx) / (distance - along))
        best = max(best, (relative - direct * along) * factor)
    if tx_slope > direct:
        # Intersect the grazing rays from the two endpoints.
        along = (rx - tx + rx_slope * distance) / (tx_slope + rx_slope)
        return (tx_slope - direct) * along * sqrt(2 * distance / (along * (distance - along)))
    return best


def _effective_profile(positions, elevations, distance):
    profile = []
    for along, ground in zip(positions, elevations, strict=True):
        remaining = distance - along
        if min(along, remaining) <= EPSILON_M:
            continue
        curvature = along * remaining / (2 * EFFECTIVE_EARTH_RADIUS_M)
        profile.append((along, ground + curvature, sqrt(2 * distance / (along * remaining))))
    return profile


def diffraction_loss_db(parameter: float, distance: float, frequency_mhz: float) -> float:
    """Convert Bullington's parameter to loss via equations 31 and 57."""
    nu = parameter * sqrt(frequency_mhz * 1e6 / LIGHT_SPEED_M_S)
    if nu <= -0.78:
        return 0.0
    edge = 6.9 + 20 * asinh(nu - 0.1) / log(10)
    return edge - expm1(-edge / 6) * (10 + 0.02 * distance / 1000)


def build_route(terrain: Terrain, index: BuildingIndex, origin: Point, target: Point, *, rooftop: str | None = None):
    if not index.contains(origin) or not index.contains(target):
        return {"status": "outside"}
    if terrain.elevation(origin) is None or terrain.elevation(target) is None:
        return {"status": "outside"}
    distance = hypot(*(a - b for a, b in zip(index.project(origin), index.project(target))))
    if distance <= EPSILON_M:
        return {"status": "coincident"}

    def elevation_at(along):
        fraction = along / distance
        return terrain.elevation(tuple(a + (b - a) * fraction for a, b in zip(origin, target)))

    spans = index.crossings(origin, target, distance)
    intervals = max(2, ceil(distance / 15))
    stations = {0.0, distance, *(distance * i / intervals for i in range(1, intervals))}
    crossings = []
    for left, right, building in spans:
        middle = (left + right) / 2
        ground = elevation_at(middle)
        stations.update((left, middle, right))
        roof = None if building.height is None else ground + building.height
        crossings.append((left, right, ground + building.base, roof, building))

    positions = sorted(stations)
    elevations = [elevation_at(along) for along in positions]
    reference = elevations[0]
    target_height = elevations[-1] - reference + RECEIVER_M

    def obstruction_mask(left, right, base, roof):
        if roof is None:
            return 0
        mask = 0
        for i, height in enumerate(HEIGHTS_M):
            tx = reference + height
            slope = (reference + target_height - tx) / distance
            low, high = sorted((tx + left * slope, tx + right * slope))
            if low <= roof and high >= base:
                mask |= 1 << i
        return mask

    result = {
        "distance": round(distance, 2),
        "targetHeight": round(target_height, 2),
        # Chart samples have uniform spacing. Extra roof edges are used only by the model.
        "ground": [round(10 * (elevation_at(distance * i / intervals) - reference)) for i in range(intervals + 1)],
        "crossings": [
            [building.id, round(left, 2), round(right, 2),
             round(base - reference, 2), None if roof is None else round(roof - reference, 2),
             obstruction_mask(left, right, base, roof)]
            for left, right, base, roof, building in crossings
        ],
        "partial": len({b.id for _, _, _, roof, b in crossings if roof is None or b.base > 0}),
    }
    origin_roofs = [(b.id, roof) for left, _, _, roof, b in crossings if left <= EPSILON_M]
    site_indoors = bool(origin_roofs)
    target_indoors = any(distance - right <= EPSILON_M for _, right, *_ in crossings)
    roof_launch = site_indoors and rooftop is not None and all(
        b_id == rooftop and roof is not None for b_id, roof in origin_roofs
    )
    if target_indoors or (site_indoors and not roof_launch) or (rooftop is not None and not roof_launch):
        return {**result, "status": "endpoint", "siteIndoors": site_indoors, "targetIndoors": target_indoors}

    roofs = [(left, right, roof) for left, right, _, roof, b in crossings if roof is not None and b.base == 0]
    skyline = [
        max(ground, max((roof for left, right, roof in roofs if left <= along <= right), default=ground))
        for along, ground in zip(positions, elevations, strict=True)
    ]
    ground_profile = _effective_profile(positions, elevations, distance)
    roof_profile = _effective_profile(positions, skyline, distance) if roofs else ground_profile
    rx = elevations[-1] + RECEIVER_M
    losses = {mhz: [] for mhz in FREQUENCIES_MHZ}
    origin_roof = max((roof for _, roof in origin_roofs), default=reference)
    for height in HEIGHTS_M:
        if roof_launch and reference + height <= origin_roof:
            for mhz in FREQUENCIES_MHZ:
                losses[mhz].append(None)
            continue
        with_ground = _bullington_parameter(ground_profile, distance, reference + height, rx)
        with_roofs = (
            _bullington_parameter(roof_profile, distance, reference + height, rx) if roofs else with_ground
        )
        for mhz in FREQUENCIES_MHZ:
            ground_db = diffraction_loss_db(with_ground, distance, mhz)
            roof_db = diffraction_loss_db(with_roofs, distance, mhz)
            losses[mhz].append([round(ground_db, 1), round(roof_db, 1), round(roof_db - ground_db, 1)])
    return {**result, "status": "modeled", "losses": losses}


def build_atlas(data, scene, terrain_data):
    if not terrain_data or not scene["buildings"]:
        return {}
    index, terrain = BuildingIndex(scene), Terrain.from_data(terrain_data)
    cells = {}
    for layer in data["layers"].values():
        for cell in layer:
            if cell["state"] == "weak":
                (south, west), (north, east) = cell["bounds"]
                cells[cell["id"]] = ((south + north) / 2, (west + east) / 2)
    candidates = {site["id"]: site for site in data["candidates"] if site["eligible"]}
    routes = {
        site: {cell: build_route(terrain, index, (info["lat"], info["lon"]), target) for cell, target in cells.items()}
        for site, info in candidates.items()
    }
    examples = [
        (site, cell, route) for site, paths in routes.items() for cell, route in paths.items()
        if route["status"] == "modeled" and not route["partial"] and route["crossings"]
        and cell in candidates[site]["covered_by_period"]["all"]
    ]
    low, high = HEIGHTS_M.index(25), HEIGHTS_M.index(40)
    # Pick a short, intelligible real example with a material roof effect.
    examples = [
        (site, cell, route) for site, cell, route in examples
        if route["losses"][1800][low][2] - route["losses"][1800][high][2] >= 6
        and any(mask & (1 << low) for *_, mask in route["crossings"])
        and not any(mask & (1 << high) for *_, mask in route["crossings"])
    ]
    example = min(examples, key=lambda item: (len(item[2]["crossings"]), item[2]["distance"], item[:2]), default=None)
    return {
        "heights": HEIGHTS_M, "receiverHeight": RECEIVER_M, "limit": SCREENING_LIMIT_DB,
        "routes": routes, "example": list(example[:2]) if example else [],
        "pairs": compare_pairs(
            data["layers"], candidates, routes, data["result"]["best_candidates"],
            FREQUENCIES_MHZ, HEIGHTS_M, SCREENING_LIMIT_DB,
        ),
    }
