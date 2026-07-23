"""Multi-model gridded cloud-field comparison, with the totality path
overlaid on each panel. Unlike eclipse_map.py (T31a), which only plots the
7 discrete named-site values already reduced into points.parquet, this
re-reads the raw archived GRIB2 files and renders the actual 2D field
(pcolormesh, cropped to the Iberia bbox) - a real cloud map, not dots.

Covers 5 models with a genuine full grid available: gfs, ecmwf_hres (native
total + derived L/M/H), icon_eu, icon_global (via the cached cdo remap
weights - already Iberia-cropped by that step), arpege_europe. Deliberately
excludes: aemet_harmonie (a rendered color-map IMAGE, not a numeric field -
would need its color-ramp-inversion logic, a different viz mode entirely),
ukmo_global (Open-Meteo point API, no spatial grid at all to render), and
the ensemble models (ecmwf_ens/aifs_ens/gefs_extended perturbed members,
aifs_single) - a stretch goal, not built here; the same pattern extends to
picking one representative member per family later.

Reuses the private grid-opening helpers already built and verified in
src/extract/*.py (they return full xr.Dataset/DataArray objects - the
point-reduction to a single site value is a separate step those modules do
afterward, which this module simply doesn't do) rather than re-implementing
per-format file handling a second time.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import xarray as xr

from src.config import DATA_RAW, DATA_ROOT, REPO_ROOT, eclipse_config, get_model, load_sites
from src.derive.humidity_to_cloud import derive_cloud_fractions
from src.extract.icon_extractor import (
    _ensure_remap_weights,
    _open_param_dataarray,
    _remap_icon_global_to_iberia,
)
from src.extract.icon_extractor import (
    _expected_filename as _icon_filename,
)
from src.extract.icon_extractor import (
    _valid_times_by_step as _icon_valid_times_by_step,
)
from src.extract.meteofrance_extractor import _cloud_dataset as _mf_cloud_dataset
from src.extract.meteofrance_extractor import _group_files as _mf_group_files
from src.extract.meteofrance_extractor import _step_hour_index as _mf_step_hour_index
from src.fetchers.base import format_init_dir, steps_for_run

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_ROOT / "viz"
TOTALITY_PATH_JSON = REPO_ROOT / "config" / "totality_path.json"


def _latest_run_init(model_name: str) -> datetime | None:
    d = DATA_RAW / model_name
    if not d.exists():
        return None
    dirs = sorted(
        p.name
        for p in d.iterdir()
        if p.is_dir() and p.name.isdigit() and any(p.iterdir())
    )
    if not dirs:
        return None
    return datetime.strptime(dirs[-1], "%Y%m%d%H").replace(tzinfo=None).astimezone()


def _nearest_covering_step(model_name: str, run_init: datetime) -> int | None:
    """Whichever archived step is closest to eclipse_config()'s t - reuses
    steps_for_run() and just picks the step nearest to the middle archive
    valid hour, matching eclipse_map.py's "latest run, nearest valid time"
    convention."""
    model_config = get_model(model_name)
    steps = steps_for_run(model_config, run_init)
    resolved = [s for s in steps.values() if s is not None]
    if not resolved:
        return None
    # prefer the smallest misalignment
    resolved.sort(key=lambda s: s[1])
    return resolved[0][0]


def _crop(lats: np.ndarray, lons: np.ndarray, values: np.ndarray, bbox: dict):
    lat_mask = (lats >= bbox["lat_min"]) & (lats <= bbox["lat_max"])
    lon_mask = (lons >= bbox["lon_min"]) & (lons <= bbox["lon_max"])
    return lats[lat_mask], lons[lon_mask], values[np.ix_(lat_mask, lon_mask)]


def _field_gfs(field: str, bbox: dict) -> tuple | None:
    from src.extract.grib_regular_extractor import _gfs_layer_datasets

    run_init = _latest_run_init("gfs")
    if run_init is None:
        return None
    step = _nearest_covering_step("gfs", run_init)
    if step is None:
        return None
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
    return lats, lons_sorted, values, run_init


def _field_ecmwf_hres(field: str, bbox: dict) -> tuple | None:
    run_init = _latest_run_init("ecmwf_hres")
    if run_init is None:
        return None
    step = _nearest_covering_step("ecmwf_hres", run_init)
    if step is None:
        return None
    out_dir = DATA_RAW / "ecmwf_hres" / format_init_dir(run_init)

    if field == "total":
        path = out_dir / f"tcc_f{step:03d}.grib2"
        if not path.exists():
            return None
        ds = xr.open_dataset(str(path), engine="cfgrib")
        var = next(iter(ds.data_vars))
        values = ds[var].values * 100.0  # native fraction -> percent
        lats, lons, values = _crop(ds.latitude.values, ds.longitude.values, values, bbox)
        return lats, lons, values, run_init

    path = out_dir / f"pl_f{step:03d}.grib2"
    if not path.exists():
        return None
    try:
        derived = derive_cloud_fractions(path)
    except Exception:
        log.exception("cloud_field_comparison: derive_cloud_fractions failed for %s", path)
        return None
    var = f"cloud_{field}"
    if var not in derived:
        return None
    lats, lons, values = _crop(
        derived.latitude.values, derived.longitude.values, derived[var].values, bbox
    )
    return lats, lons, values, run_init


def _field_icon_eu(field: str, bbox: dict) -> tuple | None:
    param = {"low": "CLCL", "mid": "CLCM", "high": "CLCH", "total": "CLCT"}[field]
    run_init = _latest_run_init("icon_eu")
    if run_init is None:
        return None
    model_config = get_model("icon_eu")
    by_step = _icon_valid_times_by_step(model_config, run_init)
    if not by_step:
        return None
    step = _nearest_covering_step("icon_eu", run_init)
    if step not in by_step:
        step = next(iter(by_step))
    url_template = model_config["source"]["url_template"]
    out_dir = DATA_RAW / "icon_eu" / format_init_dir(run_init)
    filename = _icon_filename(
        url_template, hh=run_init.strftime("%H"), yyyymmddhh=run_init.strftime("%Y%m%d%H"),
        fff=f"{step:03d}", param=param,
    )
    path = out_dir / filename
    if not path.exists():
        return None
    da = _open_param_dataarray(path, param)
    if da is None:
        return None
    lats, lons, values = _crop(da.latitude.values, da.longitude.values, da.values, bbox)
    return lats, lons, values, run_init


def _field_icon_global(field: str, bbox: dict) -> tuple | None:
    import tempfile

    param = {"low": "CLCL", "mid": "CLCM", "high": "CLCH", "total": "CLCT"}[field]
    run_init = _latest_run_init("icon_global")
    if run_init is None:
        return None
    model_config = get_model("icon_global")
    by_step = _icon_valid_times_by_step(model_config, run_init)
    if not by_step:
        return None
    step = _nearest_covering_step("icon_global", run_init)
    if step not in by_step:
        step = next(iter(by_step))
    url_template = model_config["source"]["url_template"]
    out_dir = DATA_RAW / "icon_global" / format_init_dir(run_init)
    filename = _icon_filename(
        url_template, hh=run_init.strftime("%H"), yyyymmddhh=run_init.strftime("%Y%m%d%H"),
        fff=f"{step:03d}", param=param,
    )
    src_path = out_dir / filename
    if not src_path.exists():
        return None
    grid_path, weights_path = _ensure_remap_weights()
    with tempfile.TemporaryDirectory(prefix="cloud_field_") as tmp:
        remapped = _remap_icon_global_to_iberia(src_path, bbox, grid_path, weights_path, Path(tmp))
        da = _open_param_dataarray(remapped, param)
        if da is None:
            return None
        da = da.load()
    # Already cropped by -sellonlatbox during the remap; no further crop needed.
    return da.latitude.values, da.longitude.values, da.values, run_init


def _field_arpege(field: str, bbox: dict) -> tuple | None:
    if field == "total":
        return None  # SP2 package has no native total field, only lcc/mcc/hcc
    var = {"low": "lcc", "mid": "mcc", "high": "hcc"}[field]
    run_init = _latest_run_init("arpege_europe")
    if run_init is None:
        return None
    files = _mf_group_files("arpege_europe", run_init)
    if not files:
        return None
    step = _nearest_covering_step("arpege_europe", run_init)
    for path in files:
        ds = _mf_cloud_dataset(path)
        if ds is None:
            continue
        idx_by_step = _mf_step_hour_index(ds)
        if step in idx_by_step:
            field_ds = ds.isel(step=idx_by_step[step])
            lats, lons, values = _crop(
                field_ds.latitude.values, field_ds.longitude.values, field_ds[var].values, bbox
            )
            return lats, lons, values, run_init
    return None


_MODEL_READERS = {
    "gfs": _field_gfs,
    "ecmwf_hres": _field_ecmwf_hres,
    "icon_eu": _field_icon_eu,
    "icon_global": _field_icon_global,
    "arpege_europe": _field_arpege,
}


def plot_comparison(field: str = "total", output_path: Path | None = None) -> Path:
    bbox = eclipse_config()["bbox"]
    sites = load_sites()["sites"]
    with open(TOTALITY_PATH_JSON, encoding="utf-8") as f:
        path_data = json.load(f)

    panels = []
    for model_name, reader in _MODEL_READERS.items():
        try:
            result = reader(field, bbox)
        except Exception:
            log.exception("cloud_field_comparison: %s/%s failed", model_name, field)
            result = None
        panels.append((model_name, result))

    n = len(panels)
    ncols = 3
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 5 * nrows), squeeze=False)

    north, south = path_data["northLimit"], path_data["southLimit"]
    band_lon = [p["lon"] for p in north] + [p["lon"] for p in reversed(south)]
    band_lat = [p["lat"] for p in north] + [p["lat"] for p in reversed(south)]

    for i, (model_name, result) in enumerate(panels):
        ax = axes[i // ncols][i % ncols]
        if result is None:
            ax.text(
                0.5, 0.5, f"{model_name}\n(no data)",
                ha="center", va="center", color="red", transform=ax.transAxes,
            )
            ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
            ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
        else:
            lats, lons, values, run_init = result
            mesh = ax.pcolormesh(
                lons, lats, values, cmap="Blues", vmin=0, vmax=100,
                shading="auto", rasterized=True,
            )
            fig.colorbar(mesh, ax=ax, shrink=0.7, label=f"cloud_{field} (%)")
            ax.plot(band_lon, band_lat, "r-", linewidth=0.8, alpha=0.6)
            ax.scatter([s["lon"] for s in sites], [s["lat"] for s in sites],
                       marker="^", color="black", s=20, zorder=5)
            ax.set_title(f"{model_name}\nrun {run_init.strftime('%Y-%m-%d %HZ')}", fontsize=10)
        ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
        ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
        ax.set_aspect(1.3)

    for j in range(n, nrows * ncols):
        axes[j // ncols][j % ncols].axis("off")

    fig.suptitle(f"cloud_{field} across models, red line = totality central line")
    fig.tight_layout()

    output_path = output_path or (OUTPUT_DIR / f"cloud_field_comparison_{field}.svg")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    for _field in ["total", "low", "mid", "high"]:
        p = plot_comparison(_field)
        print(f"wrote {p}")
