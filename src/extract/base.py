"""Shared contract every T21 extract module builds against: the PointRow
schema (matches CLAUDE.md's data/points.parquet schema exactly), appending
rows to that file, and small helpers every format-specific extractor needs.
"""

import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from src.config import DATA_RAW, POINTS_PARQUET, load_sites

EARTH_RADIUS_KM = 6371.0088

VALID_PROVENANCE = {"native", "derived", "total_only"}


@dataclass
class PointRow:
    model: str
    run_init: datetime
    member: int  # -1 = deterministic
    site: str
    valid: datetime
    cloud_low: float | None
    cloud_mid: float | None
    cloud_high: float | None
    cloud_total: float | None
    provenance: str  # native | derived | total_only
    fetched_at: datetime

    def __post_init__(self) -> None:
        if self.provenance not in VALID_PROVENANCE:
            raise ValueError(
                f"invalid provenance {self.provenance!r}, must be one of {VALID_PROVENANCE}"
            )


def sites() -> list[dict]:
    return load_sites()["sites"]


def _destination_point(
    lat: float, lon: float, bearing_deg: float, distance_km: float
) -> tuple[float, float]:
    """Great-circle destination point (standard forward-geodesic formula on a
    spherical Earth - plenty accurate for a 100km strip at this project's
    Iberia-bbox scale, no need for an ellipsoidal geodesy library)."""
    lat1, lon1, bearing = (math.radians(v) for v in (lat, lon, bearing_deg))
    ang_dist = distance_km / EARTH_RADIUS_KM
    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang_dist)
        + math.cos(lat1) * math.sin(ang_dist) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(ang_dist) * math.cos(lat1),
        math.cos(ang_dist) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180  # normalize to -180..180


def wnw_strip_points(site: dict) -> list[dict]:
    """The WNW-sightline strip for one site (config/sites.yaml's top-level
    wnw_strip: bearing/length/interval) - one point every sample_every_km out
    to length_km, EXCLUDING the 0km point (that's the site itself, already
    covered by sites()). Each point gets a distinct name (e.g. 'Luarca_wnw25km')
    so it fits PointRow's existing `site: str` field with no schema change."""
    strip = load_sites()["wnw_strip"]
    bearing, length_km, step_km = strip["bearing_deg"], strip["length_km"], strip["sample_every_km"]
    points = []
    dist = step_km
    while dist <= length_km:
        lat, lon = _destination_point(site["lat"], site["lon"], bearing, dist)
        points.append({"name": f"{site['name']}_wnw{dist:g}km", "lat": lat, "lon": lon})
        dist += step_km
    return points


def all_sample_points() -> list[dict]:
    """Every point extractors should sample: each named site PLUS its WNW
    sightline strip (T24) - the sightline toward the low WNW sun matters as
    much as the overhead pixel, per CLAUDE.md's domain notes. Extractors that
    read a full spatial grid (GRIB2/GeoTIFF) should use this instead of
    sites() for their per-point extraction loop. NOT used by
    open_meteo_extractor.py/open_meteo_fetcher.py (ukmo_global) - Open-Meteo
    is a point API, so strip sampling there needs the FETCHER to request the
    extra coordinates too, not just extraction; not done in this pass, see
    TASKS.md T24."""
    points = list(sites())
    for site in sites():
        points.extend(wnw_strip_points(site))
    return points


def file_fetched_at(path: Path) -> datetime:
    """Proxy for 'when this was fetched' — fetchers don't persist a manifest,
    so the file's own mtime is the best available signal."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def append_points(rows: list[PointRow]) -> None:
    """Append rows to data/points.parquet (created on first call)."""
    if not rows:
        return
    df = pl.DataFrame([asdict(r) for r in rows])
    POINTS_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    if POINTS_PARQUET.exists():
        existing = pl.read_parquet(POINTS_PARQUET)
        df = pl.concat([existing, df], how="vertical_relaxed")
    df.write_parquet(POINTS_PARQUET)


def _init_dir_name(run_init: datetime) -> str:
    """Same YYYYMMDDHH convention as src/fetchers/base.py's format_init_dir -
    reimplemented locally (not imported) since importing anything under
    src.fetchers triggers its package __init__, which eagerly imports
    herbie/cfgrib and crashes on a Windows box with no ecCodes install."""
    return run_init.strftime("%Y%m%d%H")


def already_extracted(model_name: str, run_init: datetime) -> bool:
    """Idempotency check mirroring src.fetchers.base.already_fetched - has
    this run already been written to points.parquet? Needed because
    already_fetched() stays true forever once a run is on disk, so without
    a separate marker, re-extracting on every scheduler tick would append
    duplicate rows to points.parquet."""
    return (DATA_RAW / model_name / _init_dir_name(run_init) / ".extracted").exists()


def mark_extracted(model_name: str, run_init: datetime) -> None:
    marker = DATA_RAW / model_name / _init_dir_name(run_init) / ".extracted"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def nearest_gridpoint(
    ds, lat: float, lon: float, lat_dim: str = "latitude", lon_dim: str = "longitude"
):
    """Select the nearest grid cell in an xarray Dataset/DataArray to (lat, lon)."""
    return ds.sel({lat_dim: lat, lon_dim: lon}, method="nearest")

