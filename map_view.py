from __future__ import annotations

import base64
import json
from pathlib import Path
import re

from radio_profile import build_atlas
from radio_rooftop import build_rooftop_study


WEB_DIR = Path(__file__).resolve().with_name("web")
VENDOR_DIR = WEB_DIR / "vendor"


def _inline_image(match: re.Match[str]) -> str:
    image = VENDOR_DIR / "images" / match[1]
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    return f"url(data:image/png;base64,{encoded})"


def _script_with_license(name: str) -> str:
    license_text = (VENDOR_DIR / f"{name}.LICENSE").read_text(encoding="utf-8")
    script = (VENDOR_DIR / f"{name}.js").read_text(encoding="utf-8")
    # Source maps are not distributed with the standalone page.
    script = re.sub(r"^//# sourceMappingURL=.*$", "", script, flags=re.MULTILINE)
    return f"/*\n{license_text.strip()}\n*/\n{script}"


def _script_json(data: dict[str, object]) -> str:
    # An identifier or place name can contain HTML. Keep it inside the JSON script.
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", r"\u003c")


def render_map(map_data: dict[str, object]) -> str:
    template = (WEB_DIR / "map.html").read_text(encoding="utf-8")
    leaflet_css = (VENDOR_DIR / "leaflet.css").read_text(encoding="utf-8")
    basemap_path = WEB_DIR / "bishkek.geojson"
    basemap = json.loads(basemap_path.read_text(encoding="utf-8"))
    if (
        not isinstance(basemap, dict)
        or basemap.get("type") != "FeatureCollection"
        or not isinstance(basemap.get("features"), list)
    ):
        raise ValueError(f"{basemap_path}: ожидался GeoJSON FeatureCollection")
    buildings_path = WEB_DIR / "buildings.json"
    buildings = {"bounds": [], "source": "", "buildings": []}
    if buildings_path.is_file():
        buildings = json.loads(buildings_path.read_text(encoding="utf-8"))
    terrain_path = WEB_DIR / "terrain.json"
    terrain = json.loads(terrain_path.read_text(encoding="utf-8")) if terrain_path.is_file() else None
    atlas = build_atlas(map_data, buildings, terrain)
    studies = {}
    for path in sorted(WEB_DIR.glob("roof-study*.json")):
        source = json.loads(path.read_text(encoding="utf-8"))
        if study := build_rooftop_study(map_data, buildings, terrain, source):
            studies[study["cell"]] = study
    if studies:
        atlas["roofStudies"] = studies

    replacements = {
        "LEAFLET_CSS": re.sub(r"url\(images/([\w.-]+\.png)\)", _inline_image, leaflet_css),
        "APP_CSS": (WEB_DIR / "map.css").read_text(encoding="utf-8"),
        "LEAFLET_JS": _script_with_license("leaflet"),
        "HEAT_JS": _script_with_license("leaflet-heat"),
        "MAP_DATA": _script_json(map_data),
        "BASEMAP_DATA": _script_json(basemap),
        "BUILDINGS_DATA": _script_json(buildings),
        "RADIO_DATA": _script_json(atlas),
        "PROFILE_UI_JS": (WEB_DIR / "profile_ui.js").read_text(encoding="utf-8"),
        "APP_JS": (WEB_DIR / "map.js").read_text(encoding="utf-8"),
    }
    return re.sub(r"@@([A-Z_]+)@@", lambda match: replacements[match[1]], template)
