"""Tool 1's single-model/single-step map renderer.

Unlike cloud_field_comparison.py (T31c), which always renders every
registered model's LATEST run for a fixed eclipse valid time,
this module renders one arbitrary (model, run_init, step, field) frame at a
time from data/raw_latest/ - the full, un-cropped forecast range fetched by
src/fetchers/herbie_fetcher.py's/meteofrance_fetcher.py's fetch_full_range()
(see src/config.py's DATA_RAW_LATEST docstring for why that's a separate
tree from the eclipse archiver's data/raw/).

Reuses the same private grid-opening helpers already built and verified in
src/extract/*.py, same reuse rationale as cloud_field_comparison.py: no
per-format GRIB parsing is duplicated here.

Covers gfs and arome_france only, for now - the first two models wired for
Tool 1 (see TASKS.md). More models extend _MODEL_READERS the same way
cloud_field_comparison.py's own reader dict was built.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from src.config import DATA_RAW_LATEST, DATA_ROOT, eclipse_config
from src.extract.grib_regular_extractor import _gfs_layer_datasets
from src.extract.meteofrance_extractor import _cloud_dataset, _step_hour_index
from src.fetchers.base import format_init_dir
from src.viz.cloud_field_comparison import _crop

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_ROOT / "viz" / "tool1_frames"

_AROME_VAR_BY_FIELD = {"low": "lcc", "mid": "mcc", "high": "hcc"}


def _group_files_latest(model_name: str, run_init: datetime) -> list[Path]:
    """Same convention as meteofrance_extractor.py's _group_files(), but
    against DATA_RAW_LATEST - that module's own version is hardcoded to
    DATA_RAW, which isn't where fetch_full_range() writes."""
    d = DATA_RAW_LATEST / model_name / format_init_dir(run_init)
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.grib2") if p.stat().st_size > 0)


def _gfs_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    path = DATA_RAW_LATEST / "gfs" / format_init_dir(run_init) / f"f{step:03d}_cloud.grib2"
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
    for path in _group_files_latest("arome_france", run_init):
        ds = _cloud_dataset(path)
        if ds is None:
            continue
        idx = _step_hour_index(ds)
        if step in idx:
            at_step = ds.isel(step=idx[step])
            return _crop(at_step.latitude.values, at_step.longitude.values, at_step[var].values, bbox)
    return None


_MODEL_READERS = {
    "gfs": _gfs_field,
    "arome_france": _arome_field,
}


def render_frame(
    model_name: str, run_init: datetime, step: int, field: str, output_path: Path | None = None
) -> Path:
    """Render one (model, run_init, step, field) map to a PNG and return its
    path. output_path defaults to OUTPUT_DIR/{model}/{field}/{run_init}_{step}.png."""
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
    return output_path
