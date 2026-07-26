"""Two review-grid columns, attached to the *_cmap_gamma040 rows (the raw
rows are hidden - see review_grid.json's "models" list, gamma rows only):

  combined_total: for any model with native low+mid+high (8 of 10 - even
    the 2 that have no native total at all: arome_france, arpege_europe),
    combine H/M/L via the standard random-overlap formula:

        combined = 1 - (1 - L/100)(1 - M/100)(1 - H/100)   [x100 for percent]

    the same formula used earlier this session to check ecmwf_hres's
    now-abandoned derived L/M/H against its native total. Rendered with the
    same gamma=0.4 stretch as the rest of that row's cells, for visual
    consistency within the row.

  hml_composite: same models, a false-color composite showing all three
    layers AT ONCE instead of collapsing them into one amount - each
    layer gets its own color (R=High, G=Mid, B=Low), alpha-composited over
    a white "clear sky" background in High->Mid->Low order (low cloud,
    closest to the ground and most decisive for eclipse visibility,
    painted last so it visually dominates where layers overlap). Each
    channel's alpha gets the same gamma=0.4 stretch as everything else in
    the row before compositing, for the same "don't wash out low values"
    reason as the rest of this session's colormap work.

Also applied to the AIFS ENS Prob row (aifs_ens_prob_gamma040) - it has
its own Low/Mid/High cells too (P(quantity>=threshold) per layer, from
render_prob_by_quantity_experiment.py, NOT the same computation path as
the real models' cloud fractions), so it was originally missed by this
script entirely; the same combine/composite treatment applies just as
well to three probability layers as to three cloud-fraction layers.

(total_diff, a combined-vs-native comparison column, was tried and then
removed per explicit user direction - the combined_total column above is
still validated by the earlier one-off correlation checks reported in
TASKS.md's T44, just not re-rendered as its own column going forward.)

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.render_combined_total_experiment
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

from scripts.render_prob_by_quantity_experiment import _compute_prob as _compute_aifs_ens_prob
from src.config import eclipse_config
from src.viz.basemap import draw_basemap
from src.viz.frame_renderer import (
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

GAMMA = 0.40


def _read_lmh(model_id: str, run_init: datetime, step: int, bbox: dict):
    low = _MODEL_READERS[model_id]("low", run_init, step, bbox)
    mid = _MODEL_READERS[model_id]("mid", run_init, step, bbox)
    high = _MODEL_READERS[model_id]("high", run_init, step, bbox)
    if low is None or mid is None or high is None:
        return None
    lats, lons, lval = low
    _, _, mval = mid
    _, _, hval = high
    return lats, lons, lval, mval, hval


def _read_lmh_aifs_ens_prob(run_init: datetime, step: int, bbox: dict):
    low = _compute_aifs_ens_prob(run_init, step, "low", bbox)
    mid = _compute_aifs_ens_prob(run_init, step, "mid", bbox)
    high = _compute_aifs_ens_prob(run_init, step, "high", bbox)
    if low is None or mid is None or high is None:
        return None
    lats, lons, lval = low
    _, _, mval = mid
    _, _, hval = high
    return lats, lons, lval, mval, hval


def _new_fig(bbox: dict):
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    return fig, ax, axes_top


def _finish(fig, ax, bbox: dict, axes_top: float, title: str, out_path):
    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, fontsize=10)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=100)
    plt.close(fig)


def render_combined_total(
    lval, mval, hval, lats, lons, run_init: datetime, step: int, bbox: dict,
    label: str, quantity_label: str, out_dir,
):
    combined = (1 - (1 - lval / 100) * (1 - mval / 100) * (1 - hval / 100)) * 100

    norm = mcolors.PowerNorm(gamma=GAMMA, vmin=0, vmax=100)
    fig, ax, axes_top = _new_fig(bbox)
    ax.pcolormesh(lons, lats, combined, cmap="Blues", norm=norm, shading="auto", rasterized=True)
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "r-", linewidth=0.8, alpha=0.6, zorder=7)
    ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "r--", linewidth=1, alpha=0.8, zorder=7)

    valid = run_init + timedelta(hours=step)
    title = (
        f"{label} {quantity_label} (gamma={GAMMA:.1f}) - init {_fmt_dm_z(run_init)} - "
        f"valid {_fmt_dm_z(valid)} (+{step}h)"
    )
    out_path = out_dir / "combined_total" / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    _finish(fig, ax, bbox, axes_top, title, out_path)
    return str(out_path.relative_to(OUTPUT_DIR))


def render_hml_composite(
    lval, mval, hval, lats, lons, run_init: datetime, step: int, bbox: dict,
    label: str, quantity_label: str, out_dir,
):
    # Same gamma stretch as everything else in the row, applied per-channel
    # before compositing, so a thin/faint layer still shows up as a tint
    # instead of vanishing into the white background.
    r_alpha = np.clip(hval / 100, 0, 1) ** GAMMA
    g_alpha = np.clip(mval / 100, 0, 1) ** GAMMA
    b_alpha = np.clip(lval / 100, 0, 1) ** GAMMA

    canvas = np.ones(r_alpha.shape + (3,))
    # High composited first (as if farthest away), then mid, then low
    # last/on top - low cloud (or P(low) for the prob row) is the most
    # decisive layer for whether the eclipse is actually visible, so it
    # should visually win where layers overlap.
    for alpha, color in (
        (r_alpha, np.array([1.0, 0.0, 0.0])),   # high -> red
        (g_alpha, np.array([0.0, 0.65, 0.0])),  # mid  -> green
        (b_alpha, np.array([0.0, 0.3, 1.0])),   # low  -> blue
    ):
        canvas = canvas * (1 - alpha[..., None]) + color * alpha[..., None]

    lats_plot = lats
    # imshow needs ascending lat order with origin="lower" to place north
    # at the top - flip if the source grid is north-to-south descending.
    if lats_plot[0] > lats_plot[-1]:
        lats_plot = lats_plot[::-1]
        canvas = canvas[::-1, :, :]

    fig, ax, axes_top = _new_fig(bbox)
    ax.imshow(
        canvas,
        extent=(bbox["lon_min"], bbox["lon_max"], bbox["lat_min"], bbox["lat_max"]),
        origin="lower",
        aspect="auto",
        interpolation="nearest",
    )
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "k-", linewidth=0.8, alpha=0.5, zorder=7)
    ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "k--", linewidth=1, alpha=0.7, zorder=7)

    valid = run_init + timedelta(hours=step)
    title = (
        f"{label} {quantity_label} - init {_fmt_dm_z(run_init)} - "
        f"valid {_fmt_dm_z(valid)} (+{step}h)"
    )
    out_path = out_dir / "hml_composite" / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    _finish(fig, ax, bbox, axes_top, title, out_path)
    return str(out_path.relative_to(OUTPUT_DIR))


def _process_row(gamma_row, run_init, step, bbox, lmh, label, combined_label, composite_label, out_dir):
    if lmh is None:
        gamma_row["cells"]["combined_total"] = None
        gamma_row["cells"]["hml_composite"] = None
        return
    lats, lons, lval, mval, hval = lmh

    combined_path = render_combined_total(
        lval, mval, hval, lats, lons, run_init, step, bbox, label, combined_label, out_dir
    )
    gamma_row["cells"]["combined_total"] = {"image": combined_path, "h": step, "valid": gamma_row["cells"]["low"]["valid"]}
    print(out_dir.name, "combined_total ->", combined_path)

    composite_path = render_hml_composite(
        lval, mval, hval, lats, lons, run_init, step, bbox, label, composite_label, out_dir
    )
    gamma_row["cells"]["hml_composite"] = {"image": composite_path, "h": step, "valid": gamma_row["cells"]["low"]["valid"]}
    print(out_dir.name, "hml_composite ->", composite_path)


def main() -> None:
    grid_path = OUTPUT_DIR / "review_grid.json"
    grid = json.loads(grid_path.read_text())

    # Drop the removed diff column entirely.
    grid["fields"] = [f for f in grid["fields"] if f != "total_diff"]
    if "combined_total" not in grid["fields"]:
        grid["fields"].append("combined_total")
    if "hml_composite" not in grid["fields"]:
        grid["fields"].append("hml_composite")

    by_id = {m["id"]: m for m in grid["models"]}
    bbox = eclipse_config()["bbox"]

    for model_id in MODELS_WITH_LMH:
        gamma_row_id = f"{model_id}_cmap_gamma040"
        gamma_row = by_id[gamma_row_id]
        run_init = datetime.fromisoformat(gamma_row["run_init"].replace("Z", "+00:00"))
        step = gamma_row["cells"]["low"]["h"]
        lmh = _read_lmh(model_id, run_init, step, bbox)
        label = _MODEL_LABELS.get(model_id, model_id)
        _process_row(
            gamma_row, run_init, step, bbox, lmh, label,
            "Combined Total (H+M+L)", "H/M/L composite (R=High,G=Mid,B=Low)",
            OUTPUT_DIR / gamma_row_id,
        )

    # AIFS ENS Prob row - same treatment, different data source (three
    # P(quantity>=threshold) layers instead of three cloud-fraction layers).
    prob_row_id = "aifs_ens_prob_gamma040"
    if prob_row_id in by_id:
        prob_row = by_id[prob_row_id]
        run_init = datetime.fromisoformat(prob_row["run_init"].replace("Z", "+00:00"))
        step = prob_row["cells"]["low"]["h"]
        lmh = _read_lmh_aifs_ens_prob(run_init, step, bbox)
        _process_row(
            prob_row, run_init, step, bbox, lmh, "AIFS ENS Prob",
            "Combined P(any>=thr)",
            "P composite (R=Hi,G=Mid,B=Lo)",
            OUTPUT_DIR / prob_row_id,
        )

    for m in grid["models"]:
        m["cells"].pop("total_diff", None)
        m["cells"].setdefault("combined_total", None)
        m["cells"].setdefault("hml_composite", None)

    grid_path.write_text(json.dumps(grid, indent=2))
    print("review_grid.json updated")


if __name__ == "__main__":
    main()
