"""Compare pairs on a shared set of cells with complete path estimates."""

from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Pair:
    sites: tuple[str, str]
    in_radius: set[str]
    assessed: set[str]


def _pairs_for_period(candidates, routes, period):
    radius = {site: set(info["covered_by_period"][period]) for site, info in candidates.items()}
    missing = {
        site: {cell for cell in cells if routes[site][cell]["status"] != "modeled" or routes[site][cell]["partial"]}
        for site, cells in radius.items()
    }
    pairs = []
    for a, b in combinations(sorted(radius), 2):
        total = radius[a] | radius[b]
        pairs.append(Pair((a, b), total, total - (missing[a] | missing[b])))
    return pairs, radius


def _scores(pairs, radius, routes, frequency, height_index, limit):
    """Each cell uses the smallest loss among the pair's in-radius paths."""
    scores = {}
    for pair in pairs:
        terrain_pass, roof_pass, best_paths = set(), set(), {}
        for cell in pair.assessed:
            paths = [
                (routes[site][cell]["losses"][frequency][height_index], site)
                for site in pair.sites if cell in radius[site]
            ]
            if min(loss[0] for loss, _ in paths) <= limit:
                terrain_pass.add(cell)
            loss, site = min(paths, key=lambda path: (path[0][1], path[1]))
            if loss[1] <= limit:
                roof_pass.add(cell)
            best_paths[cell] = {"site": site, "terrain": loss[0], "withRoofs": loss[1]}
        scores[pair.sites] = terrain_pass, roof_pass, best_paths
    return scores


def _comparison(left, right, common, scores, official, limit):
    p1_terrain, p1_roofs, p1_paths = scores[left.sites]
    p2_terrain, p2_roofs, p2_paths = scores[right.sites]
    terrain_change = len(p2_terrain & common) - len(p1_terrain & common)
    roof_change = len(p2_roofs & common) - len(p1_roofs & common)
    if not roof_change or roof_change * terrain_change > 0:
        return None
    if roof_change < 0:
        left, right = right, left
        p1_terrain, p2_terrain = p2_terrain, p1_terrain
        p1_roofs, p2_roofs = p2_roofs, p1_roofs
        p1_paths, p2_paths = p2_paths, p1_paths

    # A controlled witness: both pairs pass on terrain, then only the alternative
    # passes with roofs. The displayed reference path itself must meet the bare limit.
    gained = (p2_roofs - p1_roofs) & p1_terrain & p2_terrain & common
    evidence = [cell for cell in gained if p1_paths[cell]["terrain"] <= limit]
    if not evidence:
        return None
    focus = min(evidence, key=lambda cell: (p2_paths[cell]["withRoofs"] - p1_paths[cell]["withRoofs"], cell))
    regional = (
        left.sites == official and left.assessed == left.in_radius and right.assessed == right.in_radius
        and len(p2_terrain) <= len(p1_terrain) and len(p2_roofs) > len(p1_roofs)
    )
    return {
        "reference": list(left.sites), "alternative": list(right.sites),
        "commonRadius": len(left.in_radius & right.in_radius), "assessed": len(common),
        "terrain": [len(p1_terrain & common), len(p2_terrain & common)],
        "roofs": [len(p1_roofs & common), len(p2_roofs & common)],
        "gained": sorted((p2_roofs - p1_roofs) & common),
        "lost": sorted((p1_roofs - p2_roofs) & common),
        "focus": {"cell": focus, "reference": p1_paths[focus], "alternative": p2_paths[focus]},
        "regional": {"terrain": [len(p1_terrain), len(p2_terrain)], "roofs": [len(p1_roofs), len(p2_roofs)]}
        if regional else None,
    }


def _path_status(route):
    if route["status"] == "modeled":
        return "heights" if route["partial"] else "ready"
    if route["status"] == "endpoint":
        return "site" if route["siteIndoors"] else "receiver"
    return route["status"]


def _official_audit(pair, radius, routes):
    if pair is None:
        return {}
    # For shared cells show the more actionable path first. Both causes remain
    # available in the cell inspector, and each cell is colored only once.
    order = ("ready", "heights", "receiver", "site", "coincident", "outside")
    return {
        cell: sorted(
            ([site, _path_status(routes[site][cell])] for site in pair.sites if cell in radius[site]),
            key=lambda path: (order.index(path[1]), path[0]),
        )
        for cell in sorted(pair.in_radius)
    }


def compare_pairs(periods, candidates, routes, baseline, frequencies, heights, limit):
    official = tuple(sorted(baseline))
    report = {}
    for period in periods:
        pairs, radius = _pairs_for_period(candidates, routes, period)
        base = next((pair for pair in pairs if pair.sites == official), None)
        comparable = [
            (a, b, common)
            for a, b in combinations(pairs, 2)
            if len(set(a.sites).intersection(b.sites)) == 1
            and (common := a.assessed & b.assessed)
        ]
        scenarios = {}
        for frequency in frequencies:
            scenarios[frequency] = []
            for i in range(len(heights)):
                scores = _scores(pairs, radius, routes, frequency, i, limit)
                options = (
                    case for a, b, common in comparable
                    if (case := _comparison(a, b, common, scores, official, limit))
                )
                best = min(options, key=lambda case: (
                    case["regional"] is None, tuple(case["reference"]) != official, -case["assessed"],
                    case["roofs"][0] - case["roofs"][1], case["reference"], case["alternative"],
                ), default=None)
                scenarios[frequency].append(best)
        report[period] = {
            "official": {"inRadius": len(base.in_radius), "assessed": len(base.assessed)} if base else None,
            "dataAudit": _official_audit(base, radius, routes),
            "scenarios": scenarios,
        }
    return report
