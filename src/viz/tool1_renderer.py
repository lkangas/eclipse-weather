"""Tool 1's single-model/single-step map renderer.

Unlike cloud_field_comparison.py (T31c), which always renders every
registered model's LATEST run for a fixed eclipse valid time,
this module renders one arbitrary (model, run_init, step, field) frame at a
time from data/raw/ - the full, un-cropped forecast range every fetcher now
fetches (see TASKS.md's 2026-07-23 archiver-consolidation note; there used
to be a separate raw_latest/ tree for this, retired the same day and
merged back into data/raw/).

Reuses the same private grid-opening helpers already built and verified in
src/extract/*.py, same reuse rationale as cloud_field_comparison.py: no
per-format GRIB parsing is duplicated here.

Covers gfs, arome_france, gefs_extended, arpege_europe, ecmwf_hres,
ecmwf_ens, aifs_single, aifs_ens, icon_eu, icon_global - every gridded model
in models.yaml except aemet_harmonie (rendered color-ramp GeoTIFF, needs its
own color-ramp-inversion path) and the Open-Meteo point-API models
(ukmo_global, gem_global, jma_gsm, cma_grapes_global - no spatial grid to
render). See _MODEL_READERS below; extend it the same way
cloud_field_comparison.py's own reader dict was built.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

import cfgrib
import matplotlib.pyplot as plt
import numpy as np

from src.config import DATA_RAW, DATA_ROOT, eclipse_config, get_model
from src.derive.humidity_to_cloud import derive_cloud_fractions
from src.extract.ecmwf_extractor import _iter_members, _percent_scale
from src.extract.grib_regular_extractor import _gefs_levels_datasets, _gfs_layer_datasets
from src.extract.icon_extractor import (
    _ensure_remap_weights,
    _open_param_dataarray,
    _remap_icon_global_to_iberia,
)
from src.extract.icon_extractor import (
    _expected_filename as _icon_filename,
)
from src.extract.meteofrance_extractor import _cloud_dataset, _group_files, _step_hour_index
from src.fetchers.base import format_init_dir
from src.viz.basemap import draw_basemap
from src.viz.cloud_field_comparison import TOTALITY_PATH_JSON, _crop

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_ROOT / "viz" / "tool1_frames"

with open(TOTALITY_PATH_JSON, encoding="utf-8") as _f:
    _TOTALITY_PATH = json.load(_f)
_TOTALITY_BAND_LON = [p["lon"] for p in _TOTALITY_PATH["northLimit"]] + [
    p["lon"] for p in reversed(_TOTALITY_PATH["southLimit"])
]
_TOTALITY_BAND_LAT = [p["lat"] for p in _TOTALITY_PATH["northLimit"]] + [
    p["lat"] for p in reversed(_TOTALITY_PATH["southLimit"])
]
_TOTALITY_CENTER_LON = [p["lon"] for p in _TOTALITY_PATH["centralLine"]]
_TOTALITY_CENTER_LAT = [p["lat"] for p in _TOTALITY_PATH["centralLine"]]

# Shared by arome_france AND arpege_europe - both are Meteo-France SP2-package
# group files with the identical lcc/mcc/hcc param names (T31c's own
# _field_arpege reuses the exact same mapping for this reason).
_AROME_VAR_BY_FIELD = {"low": "lcc", "mid": "mcc", "high": "hcc"}


def _gfs_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    path = DATA_RAW / "gfs" / format_init_dir(run_init) / f"f{step:03d}_cloud.grib2"
    if not path.exists():
        return None
    layers = _gfs_layer_datasets(path)
    ds = layers.get(field)
    if ds is None:
        return None
    var = next(iter(ds.data_vars))
    lons = ds.longitude.values.copy()
    lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    lats, lons_sorted, values = _crop(
        ds.latitude.values, lons[order], ds[var].values[:, order], bbox
    )
    return lats, lons_sorted, values


def _arome_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    if field == "total":
        return None  # SP2 has no native total field (meteofrance_extractor.py's own note)
    var = _AROME_VAR_BY_FIELD[field]
    for path in _group_files("arome_france", run_init):
        ds = _cloud_dataset(path)
        if ds is None:
            continue
        idx = _step_hour_index(ds)
        if step in idx:
            at_step = ds.isel(step=idx[step])
            return _crop(
                at_step.latitude.values, at_step.longitude.values, at_step[var].values, bbox
            )
    return None


def _arpege_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Same package/shape as arome_france (SP2, no native total field) - see
    T31c's own _field_arpege for the same reasoning."""
    if field == "total":
        return None
    var = _AROME_VAR_BY_FIELD[field]
    for path in _group_files("arpege_europe", run_init):
        ds = _cloud_dataset(path)
        if ds is None:
            continue
        idx = _step_hour_index(ds)
        if step in idx:
            at_step = ds.isel(step=idx[step])
            return _crop(
                at_step.latitude.values, at_step.longitude.values, at_step[var].values, bbox
            )
    return None


def _gefs_extended_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """gefs_extended's fetch() only ever fetches the control
    member (c00) - see herbie_fetcher.py's _MODEL_SPECS - so there is no
    ensemble `number` dimension to select here, unlike ecmwf_ens/aifs_ens
    below; every dataset opened from these files carries a single scalar
    number=0. Longitude is 0-360 (NOAA global grid), same conversion as
    _gfs_field above."""
    base_dir = DATA_RAW / "gefs_extended" / format_init_dir(run_init)

    if field == "total":
        path = base_dir / f"f{step:03d}_c00_total.grib2"
        if not path.exists():
            return None
        dsets = cfgrib.open_datasets(str(path))
        if not dsets:
            return None
        ds = dsets[0]
        var = "tcc"
    else:
        path = base_dir / f"f{step:03d}_c00_levels.grib2"
        if not path.exists():
            return None
        layers = _gefs_levels_datasets(path)
        ds = layers.get(field)
        if ds is None:
            return None
        var = "tcc"

    lons = ds.longitude.values.copy()
    lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    lats, lons_sorted, values = _crop(
        ds.latitude.values, lons[order], ds[var].values[:, order], bbox
    )
    return lats, lons_sorted, values


def _read_ecmwf_grid(path: Path, shortname: str, scale: float, bbox: dict) -> tuple | None:
    """One 2D grid for a single GRIB shortName, cropped to bbox. Grid is
    already -180..180 (ecmwf_extractor.py's own docstring) - no wraparound
    conversion needed, unlike the NOAA grids above.

    For ensemble files (ecmwf_ens/aifs_ens) this averages ACROSS ALL members
    (the ensemble mean), not one arbitrary representative member - per
    explicit user direction 2026-07-23: "the ensemble mean can be the
    aifs_ens entry to whatever quantity is selected." Same convention across
    every model this function serves, deterministic ones included - a
    deterministic file (ecmwf_hres's total, aifs_single) has exactly one
    "member", so averaging across it is a no-op, not a special case."""
    if not path.exists():
        return None
    members = _iter_members(path, shortname)
    if not members:
        return None
    stacked = np.stack([da.values for _, da in members], axis=0)
    mean_values = stacked.mean(axis=0)
    _, da0 = members[0]
    return _crop(da0.latitude.values, da0.longitude.values, mean_values * scale, bbox)


def _ecmwf_hres_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Native total (tcc) + derived L/M/H from pressure-level q/t, same
    provenance split as ecmwf_extractor.py's _extract_hres (two different
    source files, not two different rows here - Tool 1 renders one field at
    a time so there's no PointRow-style provenance-per-row concern)."""
    model_config = get_model("ecmwf_hres")
    out_dir = DATA_RAW / "ecmwf_hres" / format_init_dir(run_init)

    if field == "total":
        scale = _percent_scale(model_config["cloud"]["total"], "total")
        shortname = model_config["cloud"]["total"]["param"]
        return _read_ecmwf_grid(out_dir / f"tcc_f{step:03d}.grib2", shortname, scale, bbox)

    path = out_dir / f"pl_f{step:03d}.grib2"
    if not path.exists():
        return None
    try:
        derived = derive_cloud_fractions(path)
    except Exception:
        log.exception("tool1_renderer: ecmwf_hres derive_cloud_fractions failed for %s", path)
        return None
    var = f"cloud_{field}"
    if var not in derived:
        return None
    return _crop(derived.latitude.values, derived.longitude.values, derived[var].values, bbox)


def _ecmwf_ens_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """No native L/M/H for classic ENS (models.yaml: levels absent_in_open_data
    - that split only exists in aifs_ens, a different product) - total only."""
    if field != "total":
        return None
    model_config = get_model("ecmwf_ens")
    out_dir = DATA_RAW / "ecmwf_ens" / format_init_dir(run_init)
    scale = _percent_scale(model_config["cloud"]["total"], "total")
    shortname = model_config["cloud"]["total"]["param"]
    return _read_ecmwf_grid(out_dir / f"tcc_f{step:03d}.grib2", shortname, scale, bbox)


_AIFS_SHORTNAME_BY_FIELD = {"low": "lcc", "mid": "mcc", "high": "hcc"}


def _aifs_field(
    model_name: str, field: str, run_init: datetime, step: int, bbox: dict
) -> tuple | None:
    """Shared by aifs_single/aifs_ens - both write one cloud_f{step}.grib2
    with tcc/lcc/mcc/hcc all genuinely native (ecmwf_extractor.py's
    _aifs_rows note), unlike hres's native-total/derived-levels split."""
    model_config = get_model(model_name)
    path = DATA_RAW / model_name / format_init_dir(run_init) / f"cloud_f{step:03d}.grib2"
    if field == "total":
        scale = _percent_scale(model_config["cloud"]["total"], "total")
        shortname = model_config["cloud"]["total"]["param"]
    else:
        scale = _percent_scale(model_config["cloud"]["levels"], "levels")
        shortname = _AIFS_SHORTNAME_BY_FIELD[field]
    return _read_ecmwf_grid(path, shortname, scale, bbox)


def _aifs_single_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    return _aifs_field("aifs_single", field, run_init, step, bbox)


def _aifs_ens_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    return _aifs_field("aifs_ens", field, run_init, step, bbox)


_ICON_PARAM_BY_FIELD = {"low": "CLCL", "mid": "CLCM", "high": "CLCH", "total": "CLCT"}


def _icon_path(model_name: str, run_init: datetime, step: int, param: str) -> Path:
    """The path dwd_bz2_fetcher.py's fetch() wrote for this (step, param),
    reconstructed from models.yaml's own url_template - same convention as
    icon_extractor.py's _expected_filename()."""
    model_config = get_model(model_name)
    url_template = model_config["source"]["url_template"]
    filename = _icon_filename(
        url_template, hh=run_init.strftime("%H"), yyyymmddhh=run_init.strftime("%Y%m%d%H"),
        fff=f"{step:03d}", param=param,
    )
    return DATA_RAW / model_name / format_init_dir(run_init) / filename


def _icon_eu_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Already regular lat/lon - direct read, no remap (unlike icon_global
    below)."""
    param = _ICON_PARAM_BY_FIELD[field]
    path = _icon_path("icon_eu", run_init, step, param)
    if not path.exists():
        return None
    da = _open_param_dataarray(path, param)
    if da is None:
        return None
    return _crop(da.latitude.values, da.longitude.values, da.values, bbox)


def _icon_global_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Native icosahedral grid - reuses icon_extractor.py's cached cdo remap
    weights (already built/verified for the eclipse archiver's own
    DATA_RAW-rooted icon_global path) to remap+crop to Iberia in one call,
    same as cloud_field_comparison.py's _field_icon_global."""
    param = _ICON_PARAM_BY_FIELD[field]
    src_path = _icon_path("icon_global", run_init, step, param)
    if not src_path.exists():
        return None
    grid_path, weights_path = _ensure_remap_weights()
    with tempfile.TemporaryDirectory(prefix="tool1_icon_global_remap_") as tmp:
        remapped = _remap_icon_global_to_iberia(src_path, bbox, grid_path, weights_path, Path(tmp))
        da = _open_param_dataarray(remapped, param)
        if da is None:
            return None
        da = da.load()  # must load into memory before the temp dir is cleaned up
    # Already cropped by -sellonlatbox during the remap; no further crop needed.
    return da.latitude.values, da.longitude.values, da.values


_MODEL_READERS = {
    "gfs": _gfs_field,
    "arome_france": _arome_field,
    "arpege_europe": _arpege_field,
    "gefs_extended": _gefs_extended_field,
    "ecmwf_hres": _ecmwf_hres_field,
    "ecmwf_ens": _ecmwf_ens_field,
    "aifs_single": _aifs_single_field,
    "aifs_ens": _aifs_ens_field,
    "icon_eu": _icon_eu_field,
    "icon_global": _icon_global_field,
}


def render_frame(
    model_name: str, run_init: datetime, step: int, field: str, output_path: Path | None = None
) -> tuple[Path, bool]:
    """Render one (model, run_init, step, field) map to a PNG. Returns
    (path, has_data) - has_data is False when this specific field has no
    native data for this model (e.g. arome_france's "total") OR when this
    step isn't actually published at all (e.g. arome_france's group files
    start at +1h, not +0h, despite full_range_steps() assuming every model
    publishes a step-0 field - see TASKS.md T34 for the real case that
    surfaced this). Callers use has_data to decide whether a step is worth
    listing at all, not just whether *a* PNG got written - render_frame
    always writes ONE (a real map or a "(no data)" placeholder), but a
    placeholder is only useful to show for a field genuinely absent from an
    otherwise-real step, not for a step nothing was ever published for."""
    if model_name not in _MODEL_READERS:
        raise KeyError(f"tool1_renderer has no reader for model '{model_name}'")

    bbox = eclipse_config()["bbox"]
    try:
        result = _MODEL_READERS[model_name](field, run_init, step, bbox)
    except Exception:
        log.exception("tool1_renderer: %s/%s/+%dh/%s failed", model_name, run_init, step, field)
        result = None

    fig, ax = plt.subplots(figsize=(6, 5))
    if result is None:
        ax.text(
            0.5, 0.5, f"{model_name}\n(no data)",
            ha="center", va="center", color="red", transform=ax.transAxes,
        )
    else:
        lats, lons, values = result
        mesh = ax.pcolormesh(
            lons, lats, values, cmap="Blues", vmin=0, vmax=100,
            shading="auto", rasterized=True,
        )
        fig.colorbar(mesh, ax=ax, shrink=0.8, label=f"cloud_{field} (%)")
        # Coastline/roads/eclipse-path drawn stroke-only, on top of the
        # cloud fill - see basemap.py's docstring for why (no fill: the
        # pcolormesh above already covers the whole bbox, land included).
        draw_basemap(ax, bbox)
        ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "r-", linewidth=0.8, alpha=0.6, zorder=7)
        ax.plot(
            _TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "r--", linewidth=1, alpha=0.8, zorder=7
        )

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_title(f"{model_name}  run {run_init:%Y-%m-%d %HZ}  +{step}h  ({field})", fontsize=10)
    fig.tight_layout()

    output_path = output_path or (
        OUTPUT_DIR / model_name / field / f"{format_init_dir(run_init)}_{step:03d}.png"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=100)
    plt.close(fig)
    return output_path, result is not None
