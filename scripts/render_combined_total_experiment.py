"""Two review-grid columns, attached to the *_cmap_gamma040 rows (the raw
rows are hidden now - see generate_review_grid.py/review_grid.json's
"models" list, which only keeps the gamma rows going forward):

  combined_total: for any model with native low+mid+high (8 of 10 - even
    the 2 that have no native total at all: arome_france, arpege_europe),
    combine H/M/L via the standard random-overlap formula:

        combined = 1 - (1 - L/100)(1 - M/100)(1 - H/100)   [x100 for percent]

    the same formula used earlier this session to check ecmwf_hres's
    now-abandoned derived L/M/H against its native total. Rendered with the
    same gamma=0.4 stretch as the rest of that row's cells, for visual
    consistency within the row.

  total_diff: only for models that ALSO have a native total (6 of those
    8: gfs, gefs_extended, aifs_single, aifs_ens, icon_eu, icon_global) -
    combined_total minus native total, diverging colormap (its own
    SymLogNorm stretch, not gamma - a diverging quantity isn't the same
    kind of stretch as a 0-100 cloud fraction), title reports real
    correlation + mean|diff| against that model's own native total - the
    same diagnostic that found ecmwf_hres's derivation unreliable, now run
    for every model that can actually be checked.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.render_combined_total_experiment
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

from src.config import eclipse_config
from src.viz.basemap import draw_basemap
from src.viz.tool1_renderer import (
    OUTPUT_DIR,
    _MODEL_LABELS,
    _MODEL_READERS,
    _TOTALITY_BAND_LAT,
    _TOTALITY_BAND_LON,
    _TOTALITY_CENTER_LAT,
    _TOTALITY_CENTER_LON,
    _figure_layout,
    _fmt_dm_z,
)

MODELS_WITH_LMH = [
    "gfs", "gefs_extended", "arome_france", "arpege_europe",
    "aifs_single", "aifs_ens", "icon_eu", "icon_global",
]
MODELS_WITH_TOTAL_TOO = ["gfs", "gefs_extended", "aifs_single", "aifs_ens", "icon_eu", "icon_global"]

GAMMA = 0.40


def _read_combined(model_id: str, run_init: datetime, step: int, bbox: dict):
    low = _MODEL_READERS[model_id]("low", run_init, step, bbox)
    mid = _MODEL_READERS[model_id]("mid", run_init, step, bbox)
    high = _MODEL_READERS[model_id]("high", run_init, step, bbox)
    if low is None or mid is None or high is None:
        return None
    lats, lons, lval = low
    _, _, mval = mid
    _, _, hval = high
    combined = (1 - (1 - lval / 100) * (1 - mval / 100) * (1 - hval / 100)) * 100
    return lats, lons, combined


def render_combined_total(model_id: str, run_init: datetime, step: int, bbox: dict):
    result = _read_combined(model_id, run_init, step, bbox)
    if result is None:
        return None, None
    lats, lons, combined = result

    norm = mcolors.PowerNorm(gamma=GAMMA, vmin=0, vmax=100)
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.pcolormesh(lons, lats, combined, cmap="Blues", norm=norm, shading="auto", rasterized=True)
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

    label = _MODEL_LABELS.get(model_id, model_id)
    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"{label} Combined Total (H+M+L, gamma={GAMMA:.1f}) - init {_fmt_dm_z(run_init)} - "
        f"valid {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=10,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    out_dir = OUTPUT_DIR / f"{model_id}_cmap_gamma040" / "combined_total"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    fig.savefig(out_path, dpi=100)
    plt.close(fig)
    return str(out_path.relative_to(OUTPUT_DIR)), combined


def render_diff(model_id: str, run_init: datetime, step: int, bbox: dict, combined):
    if combined is None:
        return None
    total_result = _MODEL_READERS[model_id]("total", run_init, step, bbox)
    if total_result is None:
        return None
    lats, lons, native = total_result
    diff = combined - native
    corr = float(np.corrcoef(combined.ravel(), native.ravel())[0, 1])
    mean_abs = float(np.mean(np.abs(diff)))

    # Real diffs across the 6 models with both quantities are tiny (mean|d|
    # 0.03-1.6pp) with rare outliers up to ~25pp - a flat linear +-50 scale
    # (like a flat 0-100 linear cloud scale) washes almost everything to
    # near-white. SymLogNorm is the diverging-data equivalent of the gamma
    # stretch used for the cloud fields: linear within +-linthresh (keeps
    # the near-zero "good" range visible), log-compressed beyond it (still
    # shows the rare large outliers without needing a huge linear range).
    norm = mcolors.SymLogNorm(linthresh=0.5, vmin=-25, vmax=25, base=10)
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.pcolormesh(lons, lats, diff, cmap="RdBu_r", norm=norm, shading="auto", rasterized=True)
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "k-", linewidth=0.8, alpha=0.5, zorder=7)
    ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "k--", linewidth=1, alpha=0.7, zorder=7)
    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    label = _MODEL_LABELS.get(model_id, model_id)
    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"{label} Combined-Native (corr={corr:.2f}, mean|d|={mean_abs:.1f}pp) - "
        f"init {_fmt_dm_z(run_init)} - valid {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=9,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    out_dir = OUTPUT_DIR / f"{model_id}_cmap_gamma040" / "total_diff"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    fig.savefig(out_path, dpi=100)
    plt.close(fig)
    return str(out_path.relative_to(OUTPUT_DIR))


def main() -> None:
    grid_path = OUTPUT_DIR / "review_grid.json"
    grid = json.loads(grid_path.read_text())
    if "combined_total" not in grid["fields"]:
        grid["fields"].append("combined_total")
    if "total_diff" not in grid["fields"]:
        grid["fields"].append("total_diff")
    by_id = {m["id"]: m for m in grid["models"]}
    bbox = eclipse_config()["bbox"]

    for model_id in MODELS_WITH_LMH:
        gamma_row_id = f"{model_id}_cmap_gamma040"
        gamma_row = by_id[gamma_row_id]
        # Base row (still present in review_grid.json even if hidden from
        # display) has the picked step - low/mid/high share has_data for
        # every one of these models, so this is a safe source of the step.
        base_row = by_id[model_id]
        low_cell = base_row["cells"]["low"]
        run_init = datetime.fromisoformat(base_row["run_init"].replace("Z", "+00:00"))
        step = low_cell["h"]

        rel_path, combined = render_combined_total(model_id, run_init, step, bbox)
        gamma_row["cells"]["combined_total"] = (
            {"image": rel_path, "h": step, "valid": low_cell["valid"]} if rel_path else None
        )
        print(gamma_row_id, "combined_total ->", rel_path)

        if model_id in MODELS_WITH_TOTAL_TOO:
            diff_path = render_diff(model_id, run_init, step, bbox, combined)
            gamma_row["cells"]["total_diff"] = (
                {"image": diff_path, "h": step, "valid": low_cell["valid"]} if diff_path else None
            )
            print(gamma_row_id, "total_diff ->", diff_path)
        else:
            gamma_row["cells"]["total_diff"] = None

    for m in grid["models"]:
        m["cells"].setdefault("combined_total", None)
        m["cells"].setdefault("total_diff", None)

    grid_path.write_text(json.dumps(grid, indent=2))
    print("review_grid.json updated")


if __name__ == "__main__":
    main()
