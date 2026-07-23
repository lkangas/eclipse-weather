"""src/extract/grib_regular_extractor.py

Extractor for the two `fetch: herbie` models (gfs, gefs_extended) in
config/models.yaml -- both regular lat/lon grids fetched via
src/fetchers/herbie_fetcher.py.

Reads the exact files that fetcher writes into
data/raw/{model}/{initYYYYMMDDHH}/:

- gfs:            f{step:03d}_cloud.grib2               (single file/step)
- gefs_extended:  f{step:03d}_c00_total.grib2
                   f{step:03d}_c00_levels.grib2           (two files/step)

Both files carry cloud fields under multiple `typeOfLevel`s in the same
GRIB2 message set (surface low/mid/high cloud layers + an entire-atmosphere
total), which cfgrib cannot merge into one Dataset (heterogeneous level
types don't broadcast together) -- `cfgrib.open_datasets()` (plural) is
required, returning one Dataset per distinct (shortName, typeOfLevel)
"hypercube" found in the file. Verified live against real archived files
(2026-07-22, T21 build):

- gfs's f*_cloud.grib2 opens as 4 datasets with DISTINCT variable names
  (tcc/hcc/lcc/mcc) -- matches models.yaml's cfgrib_hint, so layers can be
  told apart by variable name alone.
- gefs_extended's f*_c00_levels.grib2 opens as 3 datasets that ALL use the
  variable name `tcc` (GRIB shortName for param TCDC) -- per models.yaml's
  T03 note, low/mid/high here are distinguished ONLY by which level-type
  coordinate is present (`lowCloudLayer` / `middleCloudLayer` /
  `highCloudLayer`), never by variable name. Matching on variable name for
  this file would silently collapse all three layers together.

Units: both gfs and gefs_extended cloud fields are native percent [0,100]
(models.yaml; live-confirmed via each variable's GRIB `units` attribute
== "%" during T21 testing) -- no *100 scaling needed here, unlike
ecmwf_hres/ecmwf_ens's [0,1] tcc fraction.

Longitude convention: these are NOAA global grids on a 0-360 longitude axis
(e.g. gfs longitude coordinate runs 0.0 .. 359.75), not -180..180. Site
lat/lon in config/sites.yaml are ordinary -180..180 values (e.g. Luarca
lon=-6.539). Calling nearest_gridpoint() with a negative longitude against
a 0..360-indexed coordinate would silently snap to a gridpoint near 0.0
instead of the correct ~353-360 range -- xarray's nearest-neighbour .sel
compares label values numerically and raises no error for this. Every site
longitude is normalized to [0, 360) with `% 360` before calling
nearest_gridpoint.

Member convention: gfs is genuinely deterministic -> member=-1 per the
PointRow schema's own "-1=det" convention. gefs_extended's only fetched
member is the control run (c00) -- a real member OF a 31-member ensemble
(models.yaml: kind: ensemble, members: 31), not "the deterministic run" --
so member=-1 would misrepresent it: a downstream consumer filtering
member==-1 to mean "no ensemble spread info available" would wrongly lump
gefs_extended's control-run row in with genuinely deterministic models
(gfs, ecmwf_hres, ...). Instead we read the actual ensemble member number
straight off the GRIB `number` coordinate (0 for control/c00; would be
1..30 for any perturbed member fetched in future) -- self-documenting, and
consistent with perturbed members naturally using member=1..30.

`valid` on every emitted row is the *archive target* valid time (one of
eclipse.archive_valid_hours_utc on eclipse_t()'s date) that steps_for_run()
resolved this step for -- not the GRIB file's own exact forecast-valid
timestamp. This is deliberate: CLAUDE.md's run-evolution ("d(Prog)/dt")
view fixes valid time and slides run_init, which only works if every run's
row for e.g. "the 18Z slot" carries the identical `valid` timestamp even
when nearest_step() had to round to a slightly misaligned step (this
happens for gefs_extended beyond ~240h, where steps only land every 6h).
The (step, misalignment_hours) pair is available via steps_for_run() for
anyone who needs the true offset; it is intentionally not part of the
PointRow schema.
"""

import logging
from datetime import datetime
from pathlib import Path

import cfgrib
import xarray as xr

from src.config import DATA_RAW
from src.extract.base import PointRow, all_sample_points, file_fetched_at, nearest_gridpoint
from src.extract.registry import register
from src.fetchers.base import format_init_dir, steps_for_run

log = logging.getLogger(__name__)

_SUPPORTED = {"gfs", "gefs_extended"}

# gfs's f*_cloud.grib2: layers told apart by distinct variable name.
_GFS_VAR_TO_LAYER = {"tcc": "total", "lcc": "low", "mcc": "mid", "hcc": "high"}

# gefs_extended's f*_c00_levels.grib2: layers told apart by which
# level-type coordinate is present (variable name is `tcc` for all three).
_GEFS_COORD_TO_LAYER = {
    "lowCloudLayer": "low",
    "middleCloudLayer": "mid",
    "highCloudLayer": "high",
}


def _lon_360(lon: float) -> float:
    """Normalize a -180..180 site longitude to this grid's 0..360 axis."""
    return lon % 360


def _gfs_layer_datasets(path: Path) -> dict[str, xr.Dataset]:
    """path -> {'total'|'low'|'mid'|'high': ds} for one gfs f{step}_cloud.grib2."""
    layers: dict[str, xr.Dataset] = {}
    for ds in cfgrib.open_datasets(str(path)):
        (var,) = ds.data_vars
        layer = _GFS_VAR_TO_LAYER.get(var)
        if layer is None:
            log.warning("gfs %s: unexpected data var %r, skipping", path, var)
            continue
        layers[layer] = ds
    return layers


def _gefs_levels_datasets(path: Path) -> dict[str, xr.Dataset]:
    """path -> {'low'|'mid'|'high': ds} for one gefs_extended f{step}_c00_levels.grib2."""
    layers: dict[str, xr.Dataset] = {}
    for ds in cfgrib.open_datasets(str(path)):
        found = [layer for coord, layer in _GEFS_COORD_TO_LAYER.items() if coord in ds.coords]
        if len(found) != 1:
            log.warning(
                "gefs_extended %s: dataset coords %s did not map to exactly one "
                "cloud layer (matched: %s), skipping",
                path,
                list(ds.coords),
                found,
            )
            continue
        layers[found[0]] = ds
    return layers


def _member_number(ds: xr.Dataset | None) -> int:
    """Ensemble member number straight off the GRIB `number` coordinate
    (0 for control/c00). Falls back to 0 if somehow absent."""
    if ds is not None and "number" in ds.coords:
        return int(ds["number"].values)
    return 0


def _read_value(ds: xr.Dataset | None, var: str, lat: float, lon_360: float) -> float | None:
    """Nearest-gridpoint value for `var`, or None if the dataset is missing
    or the value is NaN (e.g. below-ground/masked point)."""
    if ds is None:
        return None
    point = nearest_gridpoint(ds, lat, lon_360)
    val = float(point[var].values)
    return None if val != val else val  # NaN check without a numpy import


def _extract_gfs(model_config: dict, run_init: datetime) -> list[PointRow]:
    steps = steps_for_run(model_config, run_init)
    out_dir = DATA_RAW / "gfs" / format_init_dir(run_init)
    rows: list[PointRow] = []

    for valid_iso, step_info in steps.items():
        if step_info is None:
            continue
        step, _misalignment_h = step_info
        valid = datetime.fromisoformat(valid_iso)
        path = out_dir / f"f{step:03d}_cloud.grib2"
        if not path.exists():
            log.warning("gfs: expected file missing for valid=%s, skipping: %s", valid_iso, path)
            continue

        fetched_at = file_fetched_at(path)
        layers = _gfs_layer_datasets(path)
        missing = {"total", "low", "mid", "high"} - layers.keys()
        if missing:
            log.warning("gfs %s: missing layer(s) %s", path, sorted(missing))

        for site in all_sample_points():
            lon_360 = _lon_360(site["lon"])
            values = {
                layer: _read_value(layers.get(layer), var, site["lat"], lon_360)
                for var, layer in _GFS_VAR_TO_LAYER.items()
            }
            rows.append(
                PointRow(
                    model="gfs",
                    run_init=run_init,
                    member=-1,
                    site=site["name"],
                    valid=valid,
                    cloud_low=values["low"],
                    cloud_mid=values["mid"],
                    cloud_high=values["high"],
                    cloud_total=values["total"],
                    provenance="native",
                    fetched_at=fetched_at,
                )
            )
    return rows


def _extract_gefs_extended(model_config: dict, run_init: datetime) -> list[PointRow]:
    steps = steps_for_run(model_config, run_init)
    out_dir = DATA_RAW / "gefs_extended" / format_init_dir(run_init)
    rows: list[PointRow] = []

    for valid_iso, step_info in steps.items():
        if step_info is None:
            continue
        step, _misalignment_h = step_info
        valid = datetime.fromisoformat(valid_iso)
        total_path = out_dir / f"f{step:03d}_c00_total.grib2"
        levels_path = out_dir / f"f{step:03d}_c00_levels.grib2"
        if not total_path.exists() or not levels_path.exists():
            log.warning(
                "gefs_extended: expected file(s) missing for valid=%s, skipping: %s / %s",
                valid_iso,
                total_path,
                levels_path,
            )
            continue

        fetched_at = min(file_fetched_at(total_path), file_fetched_at(levels_path))

        total_dss = cfgrib.open_datasets(str(total_path))
        if len(total_dss) != 1:
            log.warning(
                "gefs_extended %s: expected exactly 1 dataset, found %d", total_path, len(total_dss)
            )
        total_ds = total_dss[0] if total_dss else None
        member = _member_number(total_ds)

        level_layers = _gefs_levels_datasets(levels_path)
        missing = {"low", "mid", "high"} - level_layers.keys()
        if missing:
            log.warning("gefs_extended %s: missing layer(s) %s", levels_path, sorted(missing))

        for site in all_sample_points():
            lon_360 = _lon_360(site["lon"])
            total_val = _read_value(total_ds, "tcc", site["lat"], lon_360)
            level_vals = {
                layer: _read_value(level_layers.get(layer), "tcc", site["lat"], lon_360)
                for layer in ("low", "mid", "high")
            }
            rows.append(
                PointRow(
                    model="gefs_extended",
                    run_init=run_init,
                    member=member,
                    site=site["name"],
                    valid=valid,
                    cloud_low=level_vals["low"],
                    cloud_mid=level_vals["mid"],
                    cloud_high=level_vals["high"],
                    cloud_total=total_val,
                    provenance="native",
                    fetched_at=fetched_at,
                )
            )
    return rows


@register("herbie")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Extract PointRows for gfs or gefs_extended from already-archived GRIB2
    files under data/raw/{model_name}/{initYYYYMMDDHH}/. Returns [] (with a
    logged warning per gap) rather than raising when some steps/files are
    missing -- a partially-archived run should still yield whatever it can.
    """
    if model_name == "gfs":
        return _extract_gfs(model_config, run_init)
    if model_name == "gefs_extended":
        return _extract_gefs_extended(model_config, run_init)
    raise KeyError(
        f"grib_regular_extractor does not know model '{model_name}'. "
        f"Supported: {sorted(_SUPPORTED)}"
    )
