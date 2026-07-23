"""Placename-picker data generator (TASKS.md "Deferred / not now" ->
"Placename-picker tool for the extraction site list").

Downloads Natural Earth's populated-places cultural layer
(ne_10m_populated_places, 1:10m resolution - the finest populated-places
layer Natural Earth publishes), clips it to the real eclipse totality band
from config/totality_path.json, and writes a small placenames.json for
src/viz/web/placename_picker.html's live threshold-slider map.

Same public-domain source family + download-once-cache-forever pattern as
src/viz/basemap.py's coastline/roads layers (this module reuses
basemap.py's own _download_and_extract()/_find_shp() helpers rather than
re-implementing them - same zip-download-extract mechanics, just a
different Natural Earth product). Cached under
DATA_ROOT/cache/naturalearth/ne_10m_populated_places/, live-verified
2026-07-24:
    https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places.zip

Field names verified by actually loading the shapefile and inspecting
gdf.columns.tolist() (CLAUDE.md constraint #6 - never build against a
guessed field name) rather than assumed from Natural Earth's docs:
  - NAME / NAMEASCII: place label (NAME kept as primary - accented forms
    like "León"/"Logroño" render fine in a UTF-8 page; NAMEASCII carried
    along as a fallback field, unused by the picker UI for now).
  - POP_MAX (int): population estimate. Real range in the full world layer
    is -99 (Natural Earth's documented "unknown population" sentinel) up to
    ~35.7M (Tokyo); every place that actually falls inside the totality
    band clip has a real positive value, but the sentinel is guarded for
    anyway since it's a documented possibility for this field generally.
  - SCALERANK (int, 0-10) and RANK_MAX (int, 0-14): both present. Per
    Natural Earth's own cultural-vector docs, SCALERANK is the
    editorially-curated "importance" tier NE itself uses to decide the
    minimum zoom level a place should appear at (0 = most
    significant/appears first, higher = less significant/appears only at
    closer zoom) - this is the "significance rank ... more reliable for
    small/historically-significant places than population alone" field
    named in TASKS.md's own spec. RANK_MAX is a related but distinct
    zoom-rank field (derived across NE's MAX_POP10/20/50/300/310 zoom-
    bucketed population columns) - both are exposed in the output JSON
    since keeping both was cheap, but SCALERANK is the one the UI's
    "significance" slider drives, matching TASKS.md's own field-naming.

Real finding worth flagging (not a bug, a property of this exact data
source): Natural Earth's 1:10m populated-places layer is a curated,
world-significance-filtered set of ~7300 places total, NOT an exhaustive
gazetteer of every village - so clipping it to the totality band (a mostly-
Atlantic-Ocean polygon that only crosses real land across northern Spain)
yields a real but SMALL list, dominated by mid-to-large Spanish cities
(Bilbao down to ~Guadalajara-sized places), not small villages. See the
generator's own run output / TASKS.md follow-up note for the real count.

Usage (inside Docker, needs geopandas/httpx/shapely - already in
/app/.venv per the eclipse-scheduler container's own basemap.py usage):
    .venv/bin/python -m scripts.generate_placename_data
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import geopandas as gpd
from shapely.geometry import Polygon

from src.config import DATA_ROOT, REPO_ROOT
from src.viz.basemap import _download_and_extract, _find_shp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_placename_data")

_PLACES_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_populated_places.zip"
_CACHE_NAME = "ne_10m_populated_places"
TOTALITY_PATH_JSON = REPO_ROOT / "config" / "totality_path.json"
OUTPUT_DIR = DATA_ROOT / "viz" / "tool1_frames"  # same served dir Tool 1/2/3 already write into
OUTPUT_PATH = OUTPUT_DIR / "placenames.json"


def _load_totality_path() -> dict:
    with open(TOTALITY_PATH_JSON, encoding="utf-8") as f:
        return json.load(f)


def _band_polygon(path_data: dict) -> Polygon:
    """Closed 'band between two bounding lines' polygon: northLimit points
    (west->east) followed by southLimit points reversed (east->west) - the
    same construction cloud_field_comparison.py's plot_comparison() already
    uses for its band_lon/band_lat overlay line, just closed into a polygon
    here instead of left as an open line, per this task's own spec."""
    north = path_data["northLimit"]
    south = path_data["southLimit"]
    coords = [(p["lon"], p["lat"]) for p in north] + [
        (p["lon"], p["lat"]) for p in reversed(south)
    ]
    poly = Polygon(coords)
    if not poly.is_valid:
        # Defensive only - real 2026-07-24 run confirmed this is_valid()
        # with the real totality_path.json data; buffer(0) is the standard
        # shapely self-intersection repair if that ever changes upstream.
        poly = poly.buffer(0)
    return poly


def _load_places() -> gpd.GeoDataFrame:
    dest_dir = _download_and_extract(_PLACES_URL, _CACHE_NAME)
    shp = _find_shp(dest_dir)
    return gpd.read_file(shp)


def main() -> None:
    path_data = _load_totality_path()
    band = _band_polygon(path_data)
    log.info("totality band polygon: bounds=%s area_deg2=%.2f", band.bounds, band.area)

    gdf = _load_places()
    log.info("ne_10m_populated_places: %d places worldwide", len(gdf))

    clipped = gpd.clip(gdf, band)
    log.info("clipped to totality band: %d real places", len(clipped))

    places = []
    for _, row in clipped.iterrows():
        pop_max = int(row["POP_MAX"])
        if pop_max < 0:
            # Natural Earth's documented "unknown population" sentinel
            # (-99 in the full world layer) - not hit by the real 2026-07-24
            # clip (every in-band row had a real positive value), guarded
            # here anyway since it's a real documented possibility for this
            # field and a raw -99 would silently corrupt a naive "min
            # population" slider domain.
            pop_max = None
        places.append({
            "name": str(row["NAME"]),
            "name_ascii": str(row["NAMEASCII"]),
            "admin0": str(row["ADM0NAME"]),
            "lat": float(row.geometry.y),
            "lon": float(row.geometry.x),
            "pop_max": pop_max,
            "scalerank": int(row["SCALERANK"]),
            "rank_max": int(row["RANK_MAX"]),
        })
    places.sort(key=lambda p: (p["pop_max"] or 0), reverse=True)

    north = path_data["northLimit"]
    south = path_data["southLimit"]
    central = path_data["centralLine"]
    minx, miny, maxx, maxy = band.bounds

    manifest = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "name": "Natural Earth ne_10m_populated_places (1:10m cultural vectors)",
            "url": _PLACES_URL,
        },
        "totality_band": {
            "north_limit": [[p["lon"], p["lat"]] for p in north],
            "south_limit": [[p["lon"], p["lat"]] for p in south],
            "central_line": [[p["lon"], p["lat"]] for p in central],
        },
        "extent": {"lon_min": minx, "lon_max": maxx, "lat_min": miny, "lat_max": maxy},
        "place_count": len(places),
        "places": places,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s (%d places)", OUTPUT_PATH, len(places))


if __name__ == "__main__":
    main()
