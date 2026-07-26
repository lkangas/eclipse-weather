"""AIFS ENS only: compute P(quantity >= threshold) separately for each of
total/low/mid/high (not just low, which is all the wired-in "prob_cloud"
field does today), each rendered both raw-linear and gamma=0.6 stretched -
locked-in final values (threshold via tool1_renderer.py's own
_PROB_CLOUD_THRESHOLD_PCT, now 10%; gamma=0.6 chosen as a milder low-end
boost than the main cloud fields' 0.4, after comparing both via the
threshold-sweep review tool, since removed).

This is a genuinely different comparison shape than the main model x field
review grid (4 quantities x 2 colormap treatments, not model x field), so
it's written to its own review_prob_grid.json and shown in its own small
table in review.html, rather than folded in as extra columns/rows of the
main grid.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.render_prob_by_quantity_experiment
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

from src.config import DATA_RAW, eclipse_config, get_model
from src.extract.ecmwf_extractor import _iter_members, _percent_scale
from src.fetchers.base import format_init_dir
from src.viz.basemap import draw_basemap
from src.viz.cloud_field_comparison import _crop
from src.viz.tool1_renderer import (
    OUTPUT_DIR,
    _PROB_CLOUD_THRESHOLD_PCT,
    _TOTALITY_BAND_LAT,
    _TOTALITY_BAND_LON,
    _TOTALITY_CENTER_LAT,
    _TOTALITY_CENTER_LON,
    _figure_layout,
    _fmt_dm_z,
)

MODEL = "aifs_ens"
STEP = 24
QUANTITY_SHORTNAMES = {"total": "tcc", "low": "lcc", "mid": "mcc", "high": "hcc"}
TREATMENTS = {
    "raw": ("raw", None),
    "gamma040": ("gamma=0.6", mcolors.PowerNorm(gamma=0.60, vmin=0, vmax=100)),
}


def _compute_prob(run_init: datetime, step: int, quantity: str, bbox: dict):
    model_config = get_model(MODEL)
    path = DATA_RAW / MODEL / format_init_dir(run_init) / f"cloud_f{step:03d}.grib2"
    if not path.exists():
        return None
    shortname = QUANTITY_SHORTNAMES[quantity]
    members = _iter_members(path, shortname)
    if not members:
        return None
    cfg_key, label = ("total", "total") if quantity == "total" else ("levels", "levels")
    scale = _percent_scale(model_config["cloud"][cfg_key], label)
    stacked = np.stack([da.values for _, da in members], axis=0) * scale
    prob_pct = (stacked >= _PROB_CLOUD_THRESHOLD_PCT).mean(axis=0) * 100.0
    _, da0 = members[0]
    return _crop(da0.latitude.values, da0.longitude.values, prob_pct, bbox)


def render(run_init: datetime, step: int, quantity: str, treatment_id: str) -> str | None:
    bbox = eclipse_config()["bbox"]
    result = _compute_prob(run_init, step, quantity, bbox)
    if result is None:
        return None

    treatment_label, norm = TREATMENTS[treatment_id]
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    lats, lons, values = result
    kwargs = {"norm": norm} if norm is not None else {"vmin": 0, "vmax": 100}
    ax.pcolormesh(lons, lats, values, cmap="Blues", shading="auto", rasterized=True, **kwargs)
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

    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"AIFS ENS P({quantity} >= {_PROB_CLOUD_THRESHOLD_PCT:.0f}%) ({treatment_label}) - "
        f"init {_fmt_dm_z(run_init)} - valid {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=10,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    out_dir = OUTPUT_DIR / "aifs_ens_prob_by_quantity" / f"{quantity}_{treatment_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_init:%Y%m%d%H}_{step:03d}.png"
    fig.savefig(out_path, dpi=100)
    plt.close(fig)
    return str(out_path.relative_to(OUTPUT_DIR))


def main() -> None:
    grid_path = OUTPUT_DIR / "review_grid.json"
    grid = json.loads(grid_path.read_text())
    aifs_ens = next(m for m in grid["models"] if m["id"] == "aifs_ens")
    run_init = datetime.fromisoformat(aifs_ens["run_init"].replace("Z", "+00:00"))
    valid = (run_init + timedelta(hours=STEP)).isoformat().replace("+00:00", "Z")

    rows = []
    for quantity in ["total", "low", "mid", "high"]:
        row = {"quantity": quantity.capitalize(), "run_init": aifs_ens["run_init"]}
        for treatment_id in TREATMENTS:
            rel_path = render(run_init, STEP, quantity, treatment_id)
            row[treatment_id] = (
                {"image": rel_path, "h": STEP, "valid": valid} if rel_path else None
            )
            print(quantity, treatment_id, "->", rel_path)
        rows.append(row)

    out = {"generated_at": grid["generated_at"], "treatments": list(TREATMENTS.keys()), "rows": rows}
    out_path = OUTPUT_DIR / "review_prob_grid.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
