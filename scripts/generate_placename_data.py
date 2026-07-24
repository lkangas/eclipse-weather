"""Placename-picker data generator (TASKS.md "Deferred / not now" ->
"Placename-picker tool for the extraction site list").

**2026-07-24 rewrite**: swapped from Natural Earth's ne_10m_populated_places
(a curated, world-significance-filtered ~7300-place set - clipped to the
totality band, that yielded only 16 real places, all mid-to-large Spanish
cities, no villages at all - see T41's own real finding in TASKS.md) to
GeoNames' per-country dump, which is what an actually comprehensive
gazetteer looks like: real villages, hamlets, and minor settlements, not
just cities. Verified live 2026-07-24 - Spain's full GeoNames dump
(download.geonames.org/export/dump/ES.zip) has 30,895 real populated-place
(feature class "P") entries nationwide; clipped to the real totality band
polygon, **16,033** genuinely fall inside it (before excluding a handful of
historical/abandoned/destroyed entries below) - three orders of magnitude
more than the old source, and the real "start with much more" the tool
needed. CC-BY-4.0 licensed, no auth needed, same "download once, cache
forever" pattern as basemap.py's Natural Earth layers, just a different
cache subdirectory.

Field format verified from GeoNames' own live readme.txt (bundled in the
same zip) rather than assumed - 19 tab-separated columns per the
'geoname' table schema: geonameid, name, asciiname, alternatenames,
latitude, longitude, feature class, feature code, country code, cc2,
admin1 code, admin2 code, admin3 code, admin4 code, population, elevation,
dem, timezone, modification date.

Feature-code filtering (real, documented judgment call, not silent):
kept everything under feature class "P" (all populated-place subtypes)
EXCEPT four codes that GeoNames' own featureCodes.txt documents as
no-longer-real places, which have no business being a candidate
eclipse-viewing/extraction site: PPLQ (abandoned), PPLW (destroyed),
PPLH (historical, no longer exists), PPLCH (historical capital, no
longer exists). Live-verified counts inside the totality band, 2026-07-24:
16033 total P-class, of which 162 fall into those four excluded codes
(153 PPLQ + 5 PPLW + 3 PPLH + 1 PPLCH) - leaves 15871 real, current places.
PPLX ("section of populated place" - a named part/neighborhood of a larger
place, 222 of them in-band) is deliberately KEPT, not excluded - erring
toward more candidates per this task's own direction, and a PPLX entry in
this mostly-rural totality band is often a genuinely distinct hamlet, not
a big-city district.

No direct GeoNames equivalent of Natural Earth's editorial SCALERANK
exists, so `admin_rank` is a new, real, doc-grounded substitute: an
ordinal built from the feature code's own administrative-seniority
semantics (GeoNames' featureCodes.txt: PPLC=capital, PPLA/A2/A3/A4=seat of
progressively smaller administrative divisions, PPL=ordinary populated
place with no administrative role, PPLX/locality-variants=minor/sub-place).
0 = most administratively significant, matching the same "lower is bigger"
convention the old SCALERANK slider already used - kept the UI's existing
slider semantics rather than inventing a new direction.

Population: GeoNames' own `population` field, real per-place figures
(unlike Natural Earth's world-scale POP_MAX) - 0 means "not tracked by
GeoNames" (common for small villages, not a real population of zero;
3953 of the 15871 in-band places have a real nonzero figure, the rest are
real named/located places GeoNames simply has no population count for).

Usage (inside Docker, needs geopandas/httpx/shapely - already in
/app/.venv per the eclipse-scheduler container's own basemap.py usage):
    .venv/bin/python -m scripts.generate_placename_data
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from shapely.geometry import Point, Polygon

from src.config import DATA_ROOT, REPO_ROOT, eclipse_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_placename_data")

_GEONAMES_URL = "http://download.geonames.org/export/dump/ES.zip"
_CACHE_DIR = DATA_ROOT / "cache" / "geonames"
_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=15.0, pool=15.0)
TOTALITY_PATH_JSON = REPO_ROOT / "config" / "totality_path.json"
OUTPUT_DIR = DATA_ROOT / "viz" / "tool1_frames"  # same served dir Tool 1/2/3 already write into
OUTPUT_PATH = OUTPUT_DIR / "placenames.json"

# GeoNames feature codes documented as no-longer-real places (see module
# docstring) - excluded outright, not just deprioritized.
_EXCLUDED_FEATURE_CODES = {"PPLQ", "PPLW", "PPLH", "PPLCH"}

# Administrative-seniority ordinal (0=most significant), derived from
# GeoNames' own featureCodes.txt semantics - see module docstring for why
# this replaces Natural Earth's SCALERANK.
_ADMIN_RANK_BY_FEATURE_CODE = {
    "PPLC": 0,
    "PPLA": 1,
    "PPLA2": 2,
    "PPLA3": 3,
    "PPLA4": 4,
    "PPLA5": 4,
    "PPL": 5,
}
_DEFAULT_ADMIN_RANK = 6  # PPLX and every other minor/locality variant not listed above

# Real, current places (15,871 of them, see docstring) is still too many to
# ship/render as a useful starting list - filtered at the SOURCE to genuine
# administrative seats (capital through third-order, admin_rank<=3) rather
# than shipping everything and relying on the UI's slider default alone
# (explicit user direction 2026-07-24: "filter the whole placename list,
# not just default slider value"). Ordinary villages (admin_rank 5-6, the
# overwhelming majority - 12,460 plain PPL + 276 minor/locality variants)
# are dropped entirely, not just hidden. Real in-band count at this cutoff,
# live-verified 2026-07-24: 3,135 (8 PPLA + 14 PPLA2 + 3113 PPLA3) - not
# the 265 first reported, which was a stale test artifact (a population
# filter from an earlier check was still active on top of it).
_MAX_ADMIN_RANK_INCLUDED = 3

# Final population floor, explicit user direction 2026-07-24 ("the final
# numbers are rank<=3 and population>=34000") - narrows the 3,135
# admin_rank<=3 set down further to just the real, substantial towns/
# cities in that set.
_MIN_POPULATION_INCLUDED = 34000

# Manual curation on top of the two numeric cutoffs above, same explicit
# 2026-07-24 direction - real, named exceptions the numeric filters alone
# don't capture cleanly:
#   - Mallorca: every other in-band place on the island besides Palma
#     itself is redundant for this tool's purpose (island vs. the
#     mainland totality corridor) - keep Palma only.
#   - Burriana: explicitly dropped.
#   - Metro-area clustering: Paterna/Reus/San Sebastian de los Reyes/
#     Barakaldo each sit in a tight cluster of separately-incorporated
#     but immediately-adjacent municipalities (a common Spanish pattern -
#     independent towns that are functionally one built-up area) - keep
#     only the single largest-population place in each real cluster, not
#     necessarily the named one itself. Cluster membership below was
#     determined from the REAL admin_rank<=3/pop>=34000 data (haversine
#     distance, live-checked 2026-07-24), not assumed from the names alone.
_MALLORCA_BBOX = {"lat_min": 39.2, "lat_max": 39.95, "lon_min": 2.28, "lon_max": 3.48}
_MALLORCA_KEEP = "Palma"
_EXCLUDED_NAMES = {"Burriana"}


def _download_and_extract_geonames() -> Path:
    dest_dir = _CACHE_DIR / "ES"
    if dest_dir.exists() and any(dest_dir.iterdir()):
        return dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = _CACHE_DIR / "ES.zip"
    log.info("downloading %s -> %s", _GEONAMES_URL, zip_path)
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(_GEONAMES_URL)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    zip_path.unlink()
    return dest_dir


def _load_totality_path() -> dict:
    with open(TOTALITY_PATH_JSON, encoding="utf-8") as f:
        return json.load(f)


def _band_polygon(path_data: dict) -> Polygon:
    """Closed 'band between two bounding lines' polygon: northLimit points
    (west->east) followed by southLimit points reversed (east->west) - same
    construction as before (T41's original build, itself matching
    cloud_field_comparison.py's own band_lon/band_lat overlay line)."""
    north = path_data["northLimit"]
    south = path_data["southLimit"]
    coords = [(p["lon"], p["lat"]) for p in north] + [
        (p["lon"], p["lat"]) for p in reversed(south)
    ]
    poly = Polygon(coords)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


# Explicit user direction 2026-07-24: keep only the single largest-
# population place in each real metro cluster around Paterna/Reus/San
# Sebastian de los Reyes/Barakaldo, since the named place isn't always the
# biggest. Real cluster membership determined by live haversine-distance
# check against the actual admin_rank<=3/pop>=34000 output (15km radius,
# 2026-07-24) rather than assumed from the names - each cluster's real
# members and populations:
#   Valencia (824340) cluster: Paterna (64023, 6.1km), Torrent (78543,
#     7.6km), Mislata (46131, 3.6km), Burjassot (38433, 2.5km) - Valencia
#     dwarfs all four, keep Valencia only.
#   Tarragona (141542) cluster: Reus (103477, 12.3km) - Tarragona is
#     larger, keep Tarragona only.
#   Alcobendas (116037) cluster: San Sebastian de los Reyes (75912,
#     1.5km), Tres Cantos (41896, 8.5km) - Alcobendas is larger, keep
#     Alcobendas only.
#   Bilbao (347342) cluster: Barakaldo (100435, 6.3km), Santurtzi (46978,
#     5.1km), Portugalete (45826, 3.8km), Basauri (42657, 10.4km) - Bilbao
#     dwarfs all four, keep Bilbao only.
_METRO_CLUSTER_DROP_NAMES = {
    "Paterna", "Torrent", "Mislata", "Burjassot",  # -> Valencia
    "Reus",  # -> Tarragona
    "San Sebastián de los Reyes", "Tres Cantos",  # -> Alcobendas
    "Barakaldo", "Santurtzi", "Portugalete", "Basauri",  # -> Bilbao
}


def _dedupe_metro_clusters(places: list[dict]) -> list[dict]:
    return [p for p in places if p["name"] not in _METRO_CLUSTER_DROP_NAMES]


def _load_geonames_places(band: Polygon) -> list[dict]:
    dest_dir = _download_and_extract_geonames()
    txt_path = dest_dir / "ES.txt"
    places = []
    excluded_count = 0
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts[6] != "P":  # feature class - only populated places
                continue
            feature_code = parts[7]
            lat, lon = float(parts[4]), float(parts[5])
            if not band.contains(Point(lon, lat)):
                continue
            if feature_code in _EXCLUDED_FEATURE_CODES:
                excluded_count += 1
                continue
            admin_rank = _ADMIN_RANK_BY_FEATURE_CODE.get(feature_code, _DEFAULT_ADMIN_RANK)
            if admin_rank > _MAX_ADMIN_RANK_INCLUDED:
                continue
            population = int(parts[14]) if parts[14] else 0
            if population < _MIN_POPULATION_INCLUDED:
                continue
            name = parts[1]
            if name in _EXCLUDED_NAMES:
                continue
            if (
                _MALLORCA_BBOX["lat_min"] <= lat <= _MALLORCA_BBOX["lat_max"]
                and _MALLORCA_BBOX["lon_min"] <= lon <= _MALLORCA_BBOX["lon_max"]
                and name != _MALLORCA_KEEP
            ):
                continue
            places.append({
                "name": name,
                "name_ascii": parts[2],
                "lat": lat,
                "lon": lon,
                "population": population,
                "feature_code": feature_code,
                "admin_rank": admin_rank,
            })
    log.info(
        "GeoNames ES.txt: %d in-band places kept, %d excluded (historical/abandoned/destroyed)",
        len(places), excluded_count,
    )
    return _dedupe_metro_clusters(places)


def main() -> None:
    path_data = _load_totality_path()
    band = _band_polygon(path_data)
    log.info("totality band polygon: bounds=%s area_deg2=%.2f", band.bounds, band.area)

    places = _load_geonames_places(band)
    places.sort(key=lambda p: p["population"], reverse=True)

    north = path_data["northLimit"]
    south = path_data["southLimit"]
    central = path_data["centralLine"]

    # Display extent: the project's OWN established Iberia bbox
    # (config/models.yaml's eclipse.bbox, 36-44N/-10-5E - the same one
    # every Tool 1/2/3 map already renders against), not the totality
    # band polygon's own raw bounding box. The polygon's bounds reach as
    # far as ~51.6N over open Atlantic at its NW corner (this window's
    # path continues northeast past Spain) - real content (every actual
    # place) sits entirely within 39.5-43.5N, comfortably inside the
    # established bbox, so reusing it avoids a mostly-empty-ocean map
    # that's far taller than the real data needs (T41 follow-up: "limit
    # the northern extent... does not have to be square").
    display_bbox = eclipse_config()["bbox"]

    manifest = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "name": "GeoNames ES.zip (per-country populated-place dump, CC-BY-4.0)",
            "url": _GEONAMES_URL,
        },
        "totality_band": {
            "north_limit": [[p["lon"], p["lat"]] for p in north],
            "south_limit": [[p["lon"], p["lat"]] for p in south],
            "central_line": [[p["lon"], p["lat"]] for p in central],
        },
        "extent": {
            "lon_min": display_bbox["lon_min"], "lon_max": display_bbox["lon_max"],
            "lat_min": display_bbox["lat_min"], "lat_max": display_bbox["lat_max"],
        },
        "place_count": len(places),
        "places": places,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s (%d places)", OUTPUT_PATH, len(places))


if __name__ == "__main__":
    main()
