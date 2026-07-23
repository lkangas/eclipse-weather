"""Shared contract every T21 extract module builds against: the PointRow
schema (matches CLAUDE.md's data/points.parquet schema exactly), appending
rows to that file, and small helpers every format-specific extractor needs.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from src.config import DATA_RAW, POINTS_PARQUET, load_sites

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

