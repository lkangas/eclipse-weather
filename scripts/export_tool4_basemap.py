"""One-time vector-basemap export for Tool 4's clickable site-picker map.

Reuses src/viz/basemap.py's Natural Earth loaders (the same coastline +
Major/Secondary road layers the PNG renders use), clips to the Iberia bbox,
simplifies, and writes lon/lat polylines the browser projects into SVG -
plus the eclipse totality band/centerline from config/totality_path.json.

Output: DATA_ROOT/viz/frames/tool4/basemap.json (immutable; regenerate only
if the bbox or source layers change).
"""

from __future__ import annotations

import json

from src.config import DATA_ROOT, eclipse_config, load_sites
from src.viz.basemap import _clip, _load_land, _load_roads

OUT = DATA_ROOT / "viz" / "frames" / "tool4" / "basemap.json"
SIMPLIFY_DEG = 0.01  # ~1 km; a small on-screen map needs no finer


def _lines_from(geoseries, tol: float) -> list[list[list[float]]]:
    out: list[list[list[float]]] = []
    for geom in geoseries:
        if geom is None or geom.is_empty:
            continue
        geom = geom.simplify(tol)
        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for p in parts:
            coords = [[round(x, 4), round(y, 4)] for (x, y) in p.coords]
            if len(coords) >= 2:
                out.append(coords)
    return out


def _totality(bbox: dict) -> dict:
    import json as _json
    from src.config import REPO_ROOT

    raw = _json.loads((REPO_ROOT / "config" / "totality_path.json").read_text(encoding="utf-8"))
    m = 1.0  # keep points within a 1-degree margin of the bbox
    def clip(pts):
        return [
            [round(p["lon"], 4), round(p["lat"], 4)] for p in pts
            if bbox["lon_min"] - m <= p["lon"] <= bbox["lon_max"] + m
            and bbox["lat_min"] - m <= p["lat"] <= bbox["lat_max"] + m
        ]
    return {
        "central": clip(raw["centralLine"]),
        "north": clip(raw["northLimit"]),
        "south": clip(raw["southLimit"]),
    }


def main() -> int:
    bbox = eclipse_config()["bbox"]
    coast = _lines_from(_clip(_load_land(), bbox).boundary, SIMPLIFY_DEG)
    major = _lines_from(_clip(_load_roads("Major Highway"), bbox).geometry, SIMPLIFY_DEG)
    secondary = _lines_from(_clip(_load_roads("Secondary Highway"), bbox).geometry, SIMPLIFY_DEG)

    sites = [
        {"name": s["name"], "lat": round(s["lat"], 4), "lon": round(s["lon"], 4)}
        for s in load_sites()["sites"]
    ]
    payload = {
        "bbox": {k: bbox[k] for k in ("lon_min", "lat_min", "lon_max", "lat_max")},
        "coast": coast,
        "roads_major": major,
        "roads_secondary": secondary,
        "totality": _totality(bbox),
        "sites": sites,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} ({size_kb:.0f} KB): "
          f"{len(coast)} coast, {len(major)} major-road, {len(secondary)} secondary-road line(s), "
          f"{len(sites)} sites, totality central/{len(payload['totality']['central'])} "
          f"n/{len(payload['totality']['north'])} s/{len(payload['totality']['south'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
