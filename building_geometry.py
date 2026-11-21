"""Intersections of neighborhood-scale paths with OpenStreetMap footprints."""

from dataclasses import dataclass
from math import cos, pi

Point = tuple[float, float]
Box = tuple[float, float, float, float]
METRES_PER_DEGREE = 6_371_000 * pi / 180
EPSILON_M = 1e-7


@dataclass(frozen=True)
class Footprint:
    id: str
    height: float | None
    base: float
    polygons: tuple[tuple[tuple[Point, ...], ...], ...]
    box: Box


def _box(points) -> Box:
    x, y = zip(*points)
    return min(x), min(y), max(x), max(y)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _edge_crossing(a: Point, b: Point) -> float:
    return a[0] - a[1] * (b[0] - a[0]) / (b[1] - a[1])


def _ring_location(ring: tuple[Point, ...], x: float) -> int:
    inside = False
    previous = ring[-1]
    for current in ring:
        if previous[1] == current[1] == 0:
            low, high = sorted((previous[0], current[0]))
            if low - EPSILON_M <= x <= high + EPSILON_M:
                return 0
        elif (previous[1] > 0) != (current[1] > 0):
            crossing = _edge_crossing(previous, current)
            if abs(crossing - x) <= EPSILON_M:
                return 0
            inside ^= crossing > x
        previous = current
    return 1 if inside else -1


def _intervals(rings: tuple[tuple[Point, ...], ...], length: float):
    cuts = {0.0, length}
    for ring in rings:
        previous = ring[-1]
        for current in ring:
            if current[1] == 0 and 0 < current[0] < length:
                cuts.add(current[0])
            if previous[1] * current[1] < 0:
                crossing = _edge_crossing(previous, current)
                if 0 < crossing < length:
                    cuts.add(crossing)
            previous = current

    outer, *holes = rings
    ordered = sorted(cuts)
    for left, right in zip(ordered, ordered[1:]):
        if right - left <= EPSILON_M:
            continue
        mid = (left + right) / 2
        if _ring_location(outer, mid) == 1 and all(_ring_location(hole, mid) == -1 for hole in holes):
            yield left, right


def _merge(intervals):
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1] + EPSILON_M:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


class BuildingIndex:
    """A small uniform spatial grid over a local metric projection."""

    def __init__(self, scene):
        self.bounds = tuple(scene["bounds"])
        west, south, east, north = self.bounds
        self.longitude_scale = METRES_PER_DEGREE * cos((south + north) * pi / 360)
        self.footprints = []
        self.bins = {}
        for building in scene["buildings"]:
            polygons = tuple(
                tuple(tuple(self.project((lat, lon)) for lon, lat in ring) for ring in polygon)
                for polygon in building["polygons"]
            )
            if not polygons:
                continue
            box = _box(point for polygon in polygons for point in polygon[0])
            footprint = Footprint(
                building["id"], building["height"], building.get("base", 0) or 0,
                polygons, box,
            )
            for coordinate in self._bins(box):
                self.bins.setdefault(coordinate, []).append(len(self.footprints))
            self.footprints.append(footprint)

    def project(self, point: Point) -> Point:
        lat, lon = point
        west, south, *_ = self.bounds
        return (lon - west) * self.longitude_scale, (lat - south) * METRES_PER_DEGREE

    @staticmethod
    def _bins(box: Box):
        x0, y0, x1, y1 = (int(value // 250) for value in box)
        return ((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))

    def contains(self, point: Point) -> bool:
        lat, lon = point
        west, south, east, north = self.bounds
        return west <= lon <= east and south <= lat <= north

    def crossings(self, origin: Point, target: Point, length: float):
        x0, y0 = self.project(origin)
        x1, y1 = self.project(target)
        box = _box(((x0, y0), (x1, y1)))
        ux, uy = (x1 - x0) / length, (y1 - y0) / length

        def rotate(point):
            dx, dy = point[0] - x0, point[1] - y0
            across = ux * dy - uy * dx
            return ux * dx + uy * dy, 0.0 if abs(across) <= EPSILON_M else across

        hits = []
        indices = {i for key in self._bins(box) for i in self.bins.get(key, ())}
        for i in indices:
            building = self.footprints[i]
            if not _overlap(building.box, box):
                continue
            intervals = []
            for polygon in building.polygons:
                rings = tuple(tuple(map(rotate, ring)) for ring in polygon)
                intervals.extend(_intervals(rings, length))
            hits.extend((left, right, building) for left, right in _merge(intervals))
        return sorted(hits, key=lambda hit: (hit[0], hit[1], hit[2].id))
