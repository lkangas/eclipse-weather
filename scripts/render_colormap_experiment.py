"""One-off experiment: render GFS and GEFS Extended with alternate
colormap/normalization treatments, back on pcolormesh (contourf's fixed-
width bands turned out not to be the fix - see conversation: GFS's total
field is 50.9% exactly 0% and 80.4% below 10%, so the real problem is
"Blues" washing near-zero values out to near-white, not the binning).

Variants tried: "Blues" with PowerNorm(gamma) at three gamma values, to pick
the right strength - gamma=1 would be plain linear (today's washed-out
behavior); lower gamma pushes low values further up the color ramp.
blues_r and viridis were tried and rejected (inverts convention / abandons
"blue = cloud" entirely) - see conversation, not reproduced here.

Writes to gfs_cmap_<variant>/ and gefs_extended_cmap_<variant>/, and
removes any previous *_contourf_* rows from review_grid.json (that
direction is abandoned) before inserting these new rows after each
model's pixelated row.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.render_colormap_experiment
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt

from src.config import eclipse_config
from src.viz.basemap import draw_basemap
from src.viz.frame_renderer import (
    OUTPUT_DIR,
    _FIELD_LABELS,
    _MODEL_LABELS,
    _MODEL_READERS,
    _TOTALITY_BAND_LAT,
    _TOTALITY_BAND_LON,
    _TOTALITY_CENTER_LAT,
    _TOTALITY_CENTER_LON,
    _figure_layout,
    _fmt_dm_z,
)

MODELS = [
    "gfs",
    "gefs_extended",
    "arome_france",
    "arpege_europe",
    "ecmwf_hres",
    "ecmwf_ens",
    "aifs_single",
    "aifs_ens",
    "icon_eu",
    "icon_global",
]

# gamma=0.4 decided on after comparing 0.25/0.4/0.6 - see conversation.
VARIANTS = {
    "gamma040": ("Blues, gamma=0.40", "Blues", mcolors.PowerNorm(gamma=0.40, vmin=0, vmax=100)),
}


def render_variant(
    model_name: str, run_init: datetime, step: int, field: str, variant_id: str
) -> str | None:
    bbox = eclipse_config()["bbox"]
    result = _MODEL_READERS[model_name](field, run_init, step, bbox)
    if result is None:
        return None

    variant_label, cmap, norm = VARIANTS[variant_id]

    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    lats, lons, values = result
    kwargs = {"norm": norm} if norm is not None else {"vmin": 0, "vmax": 100}
    ax.pcolormesh(lons, lats, values, cmap=cmap, shading="auto", rasterized=True, **kwargs)
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "r-", linewidth=0.8, alpha=0.6, zorder=7)
    ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "r--", linewidth=1, alpha=0.8, zorder=7)

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    label = _MODEL_LABELS.get(model_name, model_name)
    field_label = _FIELD_LABELS.get(field, field)
    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"{label} {field_label} ({variant_label}) - init {_fmt_dm_z(run_init)} - "
        f"valid {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=10,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    out_dir = OUTPUT_DIR / f"{model_name}_cmap_{variant_id}" / field
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    fig.savefig(out_path, dpi=100)
    plt.close(fig)
    return str(out_path.relative_to(OUTPUT_DIR))


def main() -> None:
    grid_path = OUTPUT_DIR / "review_grid.json"
    grid = json.loads(grid_path.read_text())
    by_id = {m["id"]: m for m in grid["models"]}

    new_rows_by_model = {model_id: [] for model_id in MODELS}
    for model_id in MODELS:
        model = by_id[model_id]
        run_init = datetime.fromisoformat(model["run_init"].replace("Z", "+00:00"))
        for variant_id, (variant_label, _cmap, _norm) in VARIANTS.items():
            cells = {}
            for field, cell in model["cells"].items():
                if cell is None:
                    cells[field] = None
                    continue
                step = cell["h"]
                rel_path = render_variant(model_id, run_init, step, field, variant_id)
                cells[field] = (
                    {"image": rel_path, "h": step, "valid": cell["valid"]} if rel_path else None
                )
                print(model_id, variant_id, field, "->", rel_path)
            new_rows_by_model[model_id].append(
                {
                    "id": f"{model_id}_cmap_{variant_id}",
                    "label": f"{_MODEL_LABELS.get(model_id, model_id)} ({variant_label})",
                    "run_init": model["run_init"],
                    "cells": cells,
                }
            )

    # Drop the abandoned contourf-experiment rows and this script's own
    # previous rows, then insert fresh variant rows after each pixelated row.
    out_models = []
    for m in grid["models"]:
        if any(m["id"].startswith(f"{model_id}_contourf") for model_id in MODELS):
            continue
        if any(m["id"].startswith(f"{model_id}_cmap") for model_id in MODELS):
            continue
        out_models.append(m)
        if m["id"] in MODELS:
            out_models.extend(new_rows_by_model[m["id"]])
    grid["models"] = out_models
    grid_path.write_text(json.dumps(grid, indent=2))
    print(f"updated {grid_path} with {sum(len(v) for v in new_rows_by_model.values())} colormap row(s)")


if __name__ == "__main__":
    main()
