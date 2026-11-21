"""Documented rooftop comparisons. Official coverage uses the original inputs."""

from itertools import product

from building_geometry import BuildingIndex
from radio_profile import FREQUENCIES_MHZ, HEIGHTS_M, Terrain, build_route


def build_rooftop_study(data, scene, terrain_data, study):
    if not terrain_data or not scene["buildings"] or data["result"]["best_candidates"] != study["official"]:
        return None

    cell_id = study["cell"]
    cell = next((c for c in data["layers"]["all"] if c["id"] == cell_id and c["state"] == "weak"), None)
    if cell is None:
        return None
    (south, west), (north, east) = cell["bounds"]
    target = ((south + north) / 2, (west + east) / 2)
    sites = {
        site["id"]: (site["lat"], site["lon"])
        for site in data["candidates"]
        if site["eligible"] and cell_id in site["covered_by_period"]["all"]
        and (site["id"] in study["official"] or site["id"] in study["alternative"])
    }
    # These card matches apply to this exact path, even if another dataset reuses its ID.
    points = {**sites, "target": target}
    expected = {**study["origins"], "target": study["target"]}
    if points.keys() != expected.keys() or any(
        abs(a - b) > 1e-6 for id_, coords in points.items() for a, b in zip(coords, expected[id_], strict=True)
    ):
        return None

    sources = study["sources"]
    fractions = study["storeyMetres"]
    terrain = Terrain.from_data(terrain_data)
    rooftop_site = study["rooftopSite"]

    def routes_at(storey_height, overrides, footprints):
        buildings = []
        for building in footprints:
            record = sources.get(building["id"])
            if record:
                count = overrides.get(building["id"], record["floors"])
                if count == 0:
                    continue
                building = {**building, "height": storey_height * count}
            buildings.append(building)
        index = BuildingIndex({**scene, "buildings": buildings})
        return {
            site: build_route(terrain, index, origin, target, rooftop=study["rooftop"] if site == rooftop_site else None)
            for site, origin in sites.items()
        }

    nominal = routes_at(fractions[1], {}, scene["buildings"])
    crossed = {crossing[0] for route in nominal.values() for crossing in route.get("crossings", [])}
    if set(sources) != crossed:
        return None
    # Heights and removals cannot make other contours intersect these fixed paths.
    footprints = [building for building in scene["buildings"] if building["id"] in crossed]
    ambiguous = {
        id_: (record["floors"], record["alternateFloors"])
        for id_, record in sources.items() if "alternateFloors" in record
    }
    alternatives = [
        {id_: levels for id_, levels in zip(ambiguous, combination, strict=True) if levels != sources[id_]["floors"]}
        for combination in product(*ambiguous.values())
    ]
    variants = [
        routes_at(storey_height, overrides, footprints) if storey_height != fractions[1] or overrides else nominal
        for storey_height, overrides in product(fractions, alternatives)
    ]
    if any(route["status"] != "modeled" or route["partial"] for case in variants for route in case.values()):
        return None

    def loss_ranges(site, frequency):
        trials = [case[site]["losses"][frequency] for case in variants]
        return [
            [min(v[1] for v in values), max(v[1] for v in values)] if all(v is not None for v in values) else None
            for values in zip(*trials, strict=True)
        ]

    spread = {site: {mhz: loss_ranges(site, mhz) for mhz in FREQUENCIES_MHZ} for site in sites}
    min_height = next((HEIGHTS_M[i] for i, values in enumerate(spread[rooftop_site][1800]) if values is not None), None)
    if min_height is None:
        return None
    return {
        "cell": cell_id, "official": study["official"], "alternative": study["alternative"], "sites": nominal,
        "rooftopSite": rooftop_site, "rooftop": study["rooftop"], "sources": sources,
        "storeyMetres": fractions, "spread": spread, "minHeight": min_height, "variants": len(variants),
    }
