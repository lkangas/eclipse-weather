"""Extractor for the `fetch: http_bz2` models in config/models.yaml: icon_eu
(DWD ICON-EU, already regular lat/lon) and icon_global (DWD ICON global,
native icosahedral grid -- requires a cdo remap to regular lat/lon, cropped
to the Iberia bbox, before any per-site nearest-gridpoint extraction works).

Reads the exact per-(step, param) files src/fetchers/dwd_bz2_fetcher.py
writes under data/raw/{model}/{initYYYYMMDDHH}/. Filenames are re-derived
from models.yaml's own `source.url_template` (CLAUDE.md Hard Constraint #2:
no duplicated URL/filename knowledge lives here), not hardcoded separately.

icon_global cdo remap -- what was actually tried and verified
---------------------------------------------------------------------------
icon_global ships ONLY on DWD's native icosahedral R03B07 grid on
opendata.dwd.de (models.yaml `remap.required: true`, T04) -- confirmed live
against a real fetched file via `cdo sinfo`: unstructured grid, 2,949,120
points, uuid a27b8de6-18c4-11e4-820a-b5b098c6a5c0 (the well-known R03B07
uuid). No regular-lat-lon variant exists to fetch instead.

This module uses DWD's prebuilt "EASY" weight bundle
(ICON_GLOBAL2WORLD_025_EASY.tar.bz2) rather than the full icosahedral grid
description file (icon_grid_0026_R03B07_G.nc.bz2) that from-scratch weight
generation would need. This was a verified choice, not a guess:

  - Both URLs were live HEAD-checked (2026-07-22): the grid description
    file is 937,689,167 bytes; the 025 EASY bundle is 43,938,502 bytes
    (~21x smaller) and the 0125 variant is 50,677,442 bytes. 025 (0.25 deg,
    global 1440x721) was chosen for size -- icon_global's own ~13 km native
    resolution and this project's per-site point extraction don't need
    0125's finer destination grid.
  - The bundle was downloaded and extracted with Python's stdlib `tarfile`
    in `r:bz2` mode -- there is no `bzip2` CLI in the `python:3.12-slim`
    image (`tar -xj` fails there: "Cannot exec: No such file or directory",
    confirmed live) -- and contains exactly two files:
    `target_grid_world_025.txt` (a plain cdo lonlat grid description,
    global 1440x721 @ 0.25 deg, 0..360 longitude convention) and
    `weights_icogl2world_025.nc` (a ~128 MB precomputed SCRIP-format
    weights file). `cdo sinfo` on the weights file reports src_grid_size ==
    2,949,120 -- an exact match against the real fetched file's own point
    count, confirmed compatible with the actual archived grid, not just
    plausible-looking.
  - Applying it in one step -- `cdo -f grb2 -sellonlatbox,<iberia bbox>
    -remap,<grid file>,<weights file> <input> <output>` -- against a real
    fetched icon_global CLCL file was live-tested in Docker and produced a
    61x33 (0.25 deg) Iberia-cropped GRIB2 with sane percent values (0..93
    range for that file/step). cdo silently and correctly handles the
    negative-longitude Iberia bbox (-10..5) against the weight bundle's
    0..360-longitude target grid -- confirmed: the output grid reports
    itself as -10..5 by 0.25 deg, not 350..365.
  - Output format is deliberately GRIB2 (`-f grb2`), not NetCDF (`-f nc` /
    `-f nc4`): this image has no netCDF4/h5py/scipy installed (only
    cfgrib/eccodes/xarray -- confirmed by listing site-packages), so
    `xr.open_dataset()` on a cdo-produced NetCDF file raises ("found
    matches ['netcdf4','scipy']... dependencies may not be installed",
    plus a misleading HDF5 "No such file" side-error that is NOT a real
    file-write race -- `os.path.exists()`/`os.stat()` on the exact same
    path confirmed the file was present and correctly sized throughout).
    Writing GRIB2 instead lets the same `engine="cfgrib"` reader used
    everywhere else in src/extract/ open the remapped file directly, with
    zero new dependencies.
  - The weight bundle + grid file are cached under
    data/cache/icon_global_remap/ on first use and reused on every later
    call (persists across container restarts -- same `VOLUME ["/app/data"]`
    as data/raw) -- this ~44 MB one-time download, never per run, per the
    task brief.

Units: both models' CLCL/CLCM/CLCH/CLCT are native percent [0,100].
models.yaml does not carry an explicit `units:` key on icon's cloud block
(unlike ecmwf_hres/ens's `units: fraction_0_1`) -- confirmed empirically
instead, against real files: a real icon_eu CLCT file's values run 0..100;
a real icon_global CLCL file (post-remap) runs 0..93.03. Consistent with
percent, not a 0..1 fraction. No *100 scale applied.

Provenance: always "native" -- models.yaml confirms CLCL/CLCM/CLCH/CLCT are
native model output for both icon_eu and icon_global (no derive step,
unlike ecmwf_hres). One PointRow per (site, valid time) with all four cloud
fields populated together, member=-1 (both models are `kind: deterministic`).
"""

from __future__ import annotations

import logging
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
import xarray as xr

from src.config import REPO_ROOT, eclipse_config
from src.extract.base import PointRow, all_sample_points, file_fetched_at, nearest_gridpoint
from src.extract.registry import register
from src.fetchers.base import raw_output_dir, steps_for_run

log = logging.getLogger(__name__)

_SUPPORTED = {"icon_eu", "icon_global"}

# models.yaml param name -> PointRow band field. Shared by both models --
# icon_eu and icon_global use the identical CLCL/CLCM/CLCH/CLCT param names.
_PARAM_TO_BAND = {"CLCL": "low", "CLCM": "mid", "CLCH": "high", "CLCT": "total"}

# --- icon_global cdo remap: cached weight bundle -----------------------------------
_CACHE_DIR = REPO_ROOT / "data" / "cache" / "icon_global_remap"
_EASY_BUNDLE_URL = "https://opendata.dwd.de/weather/lib/cdo/ICON_GLOBAL2WORLD_025_EASY.tar.bz2"
_GRID_FILENAME = "target_grid_world_025.txt"
_WEIGHTS_FILENAME = "weights_icogl2world_025.nc"
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_USER_AGENT = "eclipse-weather-archiver/0.1 (contact: lauri@farsight.space)"


def _cloud_params(model_config: dict) -> list[str]:
    """All cloud param names to read for this model (native L/M/H + total),
    de-duplicated, order preserved -- mirrors dwd_bz2_fetcher's own helper,
    reading the same models.yaml `cloud` block it fetched from."""
    cloud = model_config["cloud"]
    params: list[str] = []
    for p in [*cloud.get("levels", {}).get("params", []), cloud.get("total", {}).get("param")]:
        if p and p not in params:
            params.append(p)
    return params


def _expected_filename(url_template: str, *, hh: str, yyyymmddhh: str, fff: str, param: str) -> str:
    """The local filename dwd_bz2_fetcher wrote for this (step, param) --
    reconstructed from models.yaml's own url_template, same as the fetcher
    itself does, so no filename convention is duplicated separately here."""
    url = url_template.format(
        HH=hh, param_lower=param.lower(), YYYYMMDDHH=yyyymmddhh, FFF=fff, PARAM=param
    )
    return Path(url).name.removesuffix(".bz2")


def _valid_times_by_step(model_config: dict, run_init: datetime) -> dict[int, list[datetime]]:
    """Reduce steps_for_run()'s valid_time -> (step, misalignment) map down to
    step -> [valid_times], dropping valid times this run doesn't cover yet and
    de-duplicating file reads when more than one archive valid time nearest-
    maps to the same step. `valid` written to PointRow is always this archive
    TARGET valid time (matches grib_regular_extractor's/ecmwf_extractor's
    convention -- required for the run-evolution "fixed valid time, slide
    run_init" view to line rows up correctly across runs)."""
    steps_map = steps_for_run(model_config, run_init)
    by_step: dict[int, list[datetime]] = {}
    for valid_iso, resolved in steps_map.items():
        if resolved is None:
            continue
        step, _misalignment_h = resolved
        by_step.setdefault(step, []).append(datetime.fromisoformat(valid_iso))
    return by_step


def _value_at(da: xr.DataArray, lat: float, lon: float) -> float | None:
    point = nearest_gridpoint(da, lat, lon)
    value = float(point.values)
    return None if value != value else value  # NaN check without a numpy import


def _ensure_remap_weights() -> tuple[Path, Path]:
    """Download (once) + cache DWD's prebuilt icon_global -> regular-0.25deg
    remap weight bundle under data/cache/icon_global_remap/. Returns
    (grid_description_path, weights_path). See module docstring for why this
    bundle was chosen over generating weights from the full grid description
    file."""
    grid_path = _CACHE_DIR / _GRID_FILENAME
    weights_path = _CACHE_DIR / _WEIGHTS_FILENAME
    if grid_path.exists() and weights_path.exists():
        return grid_path, weights_path

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bundle_path = _CACHE_DIR / "ICON_GLOBAL2WORLD_025_EASY.tar.bz2"
    if not bundle_path.exists() or bundle_path.stat().st_size == 0:
        log.info(
            "icon_global: downloading cdo remap weight bundle (one-time, ~44MB): %s",
            _EASY_BUNDLE_URL,
        )
        with httpx.Client(
            timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        ) as client, client.stream("GET", _EASY_BUNDLE_URL) as resp:
            resp.raise_for_status()
            with open(bundle_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

    with tarfile.open(bundle_path, "r:bz2") as tf:
        tf.extractall(_CACHE_DIR)  # noqa: S202 -- trusted DWD opendata source, HEAD-checked above

    found_grid = next(_CACHE_DIR.rglob(_GRID_FILENAME), None)
    found_weights = next(_CACHE_DIR.rglob(_WEIGHTS_FILENAME), None)
    if found_grid is None or found_weights is None:
        raise RuntimeError(
            f"icon_global remap bundle at {bundle_path} did not contain expected files "
            f"{_GRID_FILENAME!r}/{_WEIGHTS_FILENAME!r} after extraction"
        )
    if found_grid != grid_path:
        found_grid.replace(grid_path)
    if found_weights != weights_path:
        found_weights.replace(weights_path)
    return grid_path, weights_path


def _remap_icon_global_to_iberia(
    src_path: Path, bbox: dict, grid_path: Path, weights_path: Path, tmp_dir: Path
) -> Path:
    """Remap one icosahedral icon_global GRIB2 file to a regular 0.25 deg
    lat/lon grid, cropped to the Iberia bbox, in a single cdo invocation.
    Output is GRIB2 (see module docstring for why, not NetCDF)."""
    out_path = tmp_dir / (src_path.stem + "_iberia.grib2")
    cmd = [
        "cdo",
        "-O",
        "-f",
        "grb2",
        f"-sellonlatbox,{bbox['lon_min']},{bbox['lon_max']},{bbox['lat_min']},{bbox['lat_max']}",
        f"-remap,{grid_path},{weights_path}",
        str(src_path),
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"cdo remap failed for {src_path}:\n{result.stderr}")
    return out_path


def _open_param_dataarray(path: Path, param: str) -> xr.DataArray | None:
    """Open one single-param GRIB2 file and return its one data variable as a
    DataArray, or None if the variable name doesn't match `param` (shouldn't
    happen for these single-field files, but fail soft rather than KeyError)."""
    ds = xr.open_dataset(str(path), engine="cfgrib")
    if param in ds.data_vars:
        return ds[param]
    if len(ds.data_vars) == 1:
        (only_var,) = ds.data_vars
        log.warning(
            "%s: expected data var %r, found %r instead -- using it anyway", path, param, only_var
        )
        return ds[only_var]
    log.warning("%s: expected data var %r, found %s -- skipping", path, param, list(ds.data_vars))
    return None


def _rows_for_step(
    model_name: str,
    run_init: datetime,
    valid_times: list[datetime],
    param_to_da: dict[str, xr.DataArray],
    fetched_at: datetime,
    site_list: list[dict],
) -> list[PointRow]:
    """One PointRow per (site, valid_time) combining whatever of CLCL/CLCM/
    CLCH/CLCT were readable for this step -- missing bands are left None
    rather than dropping the whole row (a partially-fetched step should still
    yield what it can, matching every other extractor in this package)."""
    rows: list[PointRow] = []
    for site in site_list:
        band_values: dict[str, float | None] = {}
        for param, band in _PARAM_TO_BAND.items():
            da = param_to_da.get(param)
            band_values[band] = None if da is None else _value_at(da, site["lat"], site["lon"])
        for valid in valid_times:
            rows.append(
                PointRow(
                    model=model_name,
                    run_init=run_init,
                    member=-1,
                    site=site["name"],
                    valid=valid,
                    cloud_low=band_values["low"],
                    cloud_mid=band_values["mid"],
                    cloud_high=band_values["high"],
                    cloud_total=band_values["total"],
                    provenance="native",
                    fetched_at=fetched_at,
                )
            )
    return rows


def _extract_icon_eu(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    out_dir = raw_output_dir(model_name, run_init)
    by_step = _valid_times_by_step(model_config, run_init)
    url_template = model_config["source"]["url_template"]
    params = _cloud_params(model_config)
    site_list = all_sample_points()
    hh = run_init.strftime("%H")
    yyyymmddhh = run_init.strftime("%Y%m%d%H")

    rows: list[PointRow] = []
    for step, valid_times in by_step.items():
        fff = f"{step:03d}"
        param_to_da: dict[str, xr.DataArray] = {}
        fetched_ats: list[datetime] = []
        for param in params:
            filename = _expected_filename(
                url_template, hh=hh, yyyymmddhh=yyyymmddhh, fff=fff, param=param
            )
            path = out_dir / filename
            if not path.exists():
                log.warning("%s: expected file missing for step %s, skipping param %s: %s",
                            model_name, fff, param, path)
                continue
            da = _open_param_dataarray(path, param)
            if da is not None:
                param_to_da[param] = da
            fetched_ats.append(file_fetched_at(path))

        if not param_to_da:
            continue
        fetched_at = min(fetched_ats)
        rows.extend(
            _rows_for_step(model_name, run_init, valid_times, param_to_da, fetched_at, site_list)
        )
    return rows


def _extract_icon_global(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    out_dir = raw_output_dir(model_name, run_init)
    by_step = _valid_times_by_step(model_config, run_init)
    url_template = model_config["source"]["url_template"]
    params = _cloud_params(model_config)
    site_list = all_sample_points()
    bbox = eclipse_config()["bbox"]
    hh = run_init.strftime("%H")
    yyyymmddhh = run_init.strftime("%Y%m%d%H")

    rows: list[PointRow] = []
    if not by_step:
        return rows

    grid_path, weights_path = _ensure_remap_weights()

    with tempfile.TemporaryDirectory(prefix="icon_global_remap_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for step, valid_times in by_step.items():
            fff = f"{step:03d}"
            param_to_da: dict[str, xr.DataArray] = {}
            fetched_ats: list[datetime] = []
            for param in params:
                filename = _expected_filename(
                    url_template, hh=hh, yyyymmddhh=yyyymmddhh, fff=fff, param=param
                )
                src_path = out_dir / filename
                if not src_path.exists():
                    log.warning(
                        "%s: expected file missing for step %s, skipping param %s: %s",
                        model_name, fff, param, src_path,
                    )
                    continue
                try:
                    remapped_path = _remap_icon_global_to_iberia(
                        src_path, bbox, grid_path, weights_path, tmp_dir
                    )
                except RuntimeError as e:
                    log.warning(
                        "%s: cdo remap failed for %s, skipping: %s", model_name, src_path, e
                    )
                    continue
                da = _open_param_dataarray(remapped_path, param)
                if da is not None:
                    # Load into memory before the temp file is cleaned up / reused.
                    param_to_da[param] = da.load()
                fetched_ats.append(file_fetched_at(src_path))

            if not param_to_da:
                continue
            fetched_at = min(fetched_ats)
            rows.extend(
                _rows_for_step(
                    model_name, run_init, valid_times, param_to_da, fetched_at, site_list
                )
            )
    return rows


@register("http_bz2")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Entry point per src/extract/registry.py's contract. Dispatches to
    icon_eu's direct-read path or icon_global's cdo-remap path -- see module
    docstring for both."""
    if model_name == "icon_eu":
        return _extract_icon_eu(model_name, model_config, run_init)
    if model_name == "icon_global":
        return _extract_icon_global(model_name, model_config, run_init)
    raise ValueError(
        f"icon_extractor has no extract path for model '{model_name}' (known: {sorted(_SUPPORTED)})"
    )
