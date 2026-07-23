"""Natural Earth basemap layers (coastline + major/secondary roads) for
matplotlib map renders.

Mirrors the sibling eclipse-dashboard project's own basemap.mjs/roads.mjs
choices rather than re-deriving which Natural Earth categories/resolution
to use: 1:50m land (coastline) and 1:10m roads filtered to "Major Highway"
+ "Secondary Highway" only (plain "Road" is left out there as visual noise
at this scale, and the same reasoning applies here). Same public-domain
source (naturalearthdata.com), live-verified 2026-07-23:
  https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip
  https://naciscdn.org/naturalearth/10m/cultural/ne_10m_roads.zip

Downloaded once and cached under DATA_ROOT/cache/naturalearth/ (same
caching convention as icon_extractor.py's cdo remap weights) - re-read
per process afterward, not re-downloaded.

Drawn stroke-only (no fill), on top of whatever's already on the axes -
unlike the sibling dashboard's own `.coast` style (fill: page background),
which only makes sense there because nothing sits underneath the basemap.
Here the pcolormesh cloud field is drawn FIRST and covers the whole bbox
including land, so a filled coastline would hide real data under it.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import httpx
from shapely.geometry import box

from src.config import DATA_ROOT

log = logging.getLogger(__name__)

_CACHE_DIR = DATA_ROOT / "cache" / "naturalearth"
_LAND_URL = "https://naciscdn.org/naturalearth/50m/physical/ne_50m_land.zip"
_ROADS_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_roads.zip"
_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=15.0, pool=15.0)

_land_raw: gpd.GeoDataFrame | None = None
_roads_raw: dict[str, gpd.GeoDataFrame] = {}


def _download_and_extract(url: str, name: str) -> Path:
    dest_dir = _CACHE_DIR / name
    if dest_dir.exists() and any(dest_dir.iterdir()):
        return dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = _CACHE_DIR / f"{name}.zip"
    log.info("basemap: downloading %s -> %s", url, zip_path)
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    zip_path.unlink()
    return dest_dir


def _find_shp(dest_dir: Path) -> Path:
    matches = list(dest_dir.glob("*.shp"))
    if not matches:
        raise FileNotFoundError(f"no .shp file found in {dest_dir}")
    return matches[0]


def _load_land() -> gpd.GeoDataFrame:
    global _land_raw
    if _land_raw is None:
        shp = _find_shp(_download_and_extract(_LAND_URL, "ne_50m_land"))
        _land_raw = gpd.read_file(shp)
    return _land_raw


def _load_roads(tier: str) -> gpd.GeoDataFrame:
    if tier not in _roads_raw:
        shp = _find_shp(_download_and_extract(_ROADS_URL, "ne_10m_roads"))
        gdf = gpd.read_file(shp)
        _roads_raw[tier] = gdf[gdf["type"] == tier]
    return _roads_raw[tier]


def _clip(gdf: gpd.GeoDataFrame, bbox: dict) -> gpd.GeoDataFrame:
    clip_box = box(bbox["lon_min"], bbox["lat_min"], bbox["lon_max"], bbox["lat_max"])
    return gpd.clip(gdf, clip_box)


def draw_basemap(ax, bbox: dict) -> None:
    """Coastline + major/secondary roads, clipped to bbox, drawn as thin
    reference lines on top of whatever's already on `ax`."""
    try:
        _clip(_load_land(), bbox).boundary.plot(
            ax=ax, color="black", linewidth=0.5, zorder=5
        )
        _clip(_load_roads("Secondary Highway"), bbox).plot(
            ax=ax, color="0.4", linewidth=0.3, alpha=0.35, zorder=5
        )
        _clip(_load_roads("Major Highway"), bbox).plot(
            ax=ax, color="0.4", linewidth=0.3, alpha=0.8, zorder=6
        )
    except Exception:
        log.exception("basemap: failed to draw coastline/roads, skipping")
