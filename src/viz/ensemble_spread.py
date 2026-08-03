"""Ensemble member "line cloud" (spaghetti plot) prototype.

UGLY ON PURPOSE, same prototype status as point_timeseries.py: one example
to react to, no layout/styling decided yet.

Two modes, two different x-axes
-------------------------------------------------------------------------
--mode single-run (default): the "normal" meteogram view - newest usable
run, x-axis is valid time, one line per member, read straight from raw
GRIB2 via _iter_members() (points.parquet only stores 3 valid times per
run - 15/18/21 UTC eclipse day, per CLAUDE.md's schema note - nowhere near
enough points for this view). Most useful right now, while the eclipse is
still far enough out that few runs have accumulated.

--mode run-evolution: x-axis is run_init, one fixed valid time (the one
nearest ECLIPSE_T, via run_evolution.py's own pick_fixed_valid_time() -
reused rather than re-derived). Reads points.parquet directly - no raw
GRIB needed, since this only ever touches those same 3 stored valid times.
Becomes the more useful view later, once many runs have piled up at the
same fixed valid time close to the eclipse; deliberately the individual-
member counterpart to Tool 2 (run_evolution.py), which draws a p10/median/
p90 BAND per run_init instead of full member spaghetti - see that module's
own "Ensembles -- percentile band, not spaghetti" section for why it chose
the band (less noisy in a small-multiples grid). Not a replacement for it,
just the individual-member detail for one (site, model) pair at a time.

Which models qualify
-------------------------------------------------------------------------
Reads data/points.parquet directly - no new extraction needed. aifs_ens (51
members: 1 control + 50 perturbed) and ecmwf_ens (50 members, perturbed
only - its control member is absent from ECMWF's open-data feed, see
models.yaml) are the only two models in this registry with real per-member
data already point-extracted - confirmed empirically (distinct member count
per model in points.parquet), not assumed from models.yaml's kind: ensemble
label. gefs_extended is nominally a 31-member ensemble upstream but this
project's fetcher only ever pulls its control member (member 0 only in
points.parquet), so it is NOT a candidate for a real member spread.

Neither ensemble has temperature at all (surface_temp.enabled: false in
models.yaml - a deliberate cost opt-out), so this is cloud-only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from src.config import DATA_RAW, DATA_ROOT, POINTS_PARQUET, get_model, load_sites
from src.extract.ecmwf_extractor import _iter_members, _percent_scale
from src.fetchers.base import format_init_dir
from src.viz.frame_renderer import _AIFS_SHORTNAME_BY_FIELD, _steps_for_run
from src.viz.point_timeseries import _pick_run_init
from src.viz.run_evolution import pick_fixed_valid_time

OUTPUT_DIR = DATA_ROOT / "viz" / "ensemble_spread"


def load_ensemble_frame(
    site_name: str, model_id: str, field: str, points_path: Path = POINTS_PARQUET
) -> tuple[datetime, pl.DataFrame]:
    """The valid time closest to ECLIPSE_T (Tool 2's own pick_fixed_valid_time,
    reused so both charts agree on which moment they're showing), restricted
    to one site/model/field, every member and every run_init present."""
    column = f"cloud_{field}"  # points.parquet's real column names, per CLAUDE.md's schema
    df = pl.read_parquet(points_path)
    df = df.filter(
        (pl.col("model") == model_id)
        & (pl.col("site") == site_name)
        & pl.col(column).is_not_null()
    )
    if df.is_empty():
        raise ValueError(f"no {column!r} data for model={model_id!r} site={site_name!r}")
    valid_time = pick_fixed_valid_time(df)
    return valid_time, df.filter(pl.col("valid") == valid_time).sort("run_init")


def plot_ensemble_spread(site_name: str, model_id: str, field: str, out_path: Path) -> None:
    column = f"cloud_{field}"
    valid_time, df = load_ensemble_frame(site_name, model_id, field)

    member_ids = sorted(df["member"].unique().to_list())
    fig, ax = plt.subplots(figsize=(11, 4.5))
    for m in member_ids:
        sub = df.filter(pl.col("member") == m).sort("run_init")
        if m <= 0:
            ax.plot(sub["run_init"], sub[column], color="#C44E52", linewidth=1.6, zorder=5,
                     marker="o", markersize=3, label=f"member {m} (control)")
        else:
            ax.plot(sub["run_init"], sub[column], color="#4C72B0", linewidth=0.6, alpha=0.3)

    median_df = df.group_by("run_init").agg(pl.col(column).median()).sort("run_init")
    ax.plot(median_df["run_init"], median_df[column], color="black", linewidth=2, zorder=6,
            label="median")

    ax.set_ylim(0, 100)
    ax.set_ylabel(f"cloud_{field} %")
    ax.set_xlabel("run_init")
    ax.set_title(
        f"{site_name} - {model_id} cloud_{field} at fixed valid={valid_time.isoformat()} - "
        f"{len(member_ids)} members x {df['run_init'].n_unique()} runs"
    )
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def _member_grid_path(
    model_id: str, run_init: datetime, step: int, field: str
) -> tuple[Path, str, float]:
    """(file path, GRIB shortName, percent scale) for one (model, step,
    field) - mirrors frame_renderer.py's _ecmwf_ens_field/_aifs_field
    path/shortname logic exactly, just without collapsing to the ensemble
    mean via _read_ecmwf_grid."""
    model_config = get_model(model_id)
    run_dir = DATA_RAW / model_id / format_init_dir(run_init)
    if field == "total":
        shortname = model_config["cloud"]["total"]["param"]
        scale = _percent_scale(model_config["cloud"]["total"], "total")
    else:
        if model_id == "ecmwf_ens":
            raise ValueError("ecmwf_ens has no native low/mid/high - only field='total' is valid")
        shortname = _AIFS_SHORTNAME_BY_FIELD[field]
        scale = _percent_scale(model_config["cloud"]["levels"], "levels")
    filename = "tcc" if model_id == "ecmwf_ens" else "cloud"
    return run_dir / f"{filename}_f{step:03d}.grib2", shortname, scale


def sample_member_series(
    model_id: str, run_init: datetime, field: str, site: dict
) -> dict[int, list[tuple[datetime, float]]]:
    """{member: [(valid, value), ...]} across every step of one run, reading
    raw GRIB2 directly via _iter_members (points.parquet only has 3 valid
    times per run - see module docstring - not enough for this view). One
    file open per step, same shape as point_timeseries.py's sampler, except
    every member is kept instead of only the mean."""
    model_config = get_model(model_id)
    steps = _steps_for_run(model_id, model_config, run_init)
    out: dict[int, list[tuple[datetime, float]]] = {}
    for step in steps:
        path, shortname, scale = _member_grid_path(model_id, run_init, step, field)
        if not path.exists():
            continue
        try:
            members = _iter_members(path, shortname)
        except Exception:
            continue
        if not members:
            continue
        valid = run_init + timedelta(hours=step)
        lats = members[0][1].latitude.values
        lons = members[0][1].longitude.values
        lat_idx = int(np.argmin(np.abs(lats - site["lat"])))
        lon_idx = int(np.argmin(np.abs(lons - site["lon"])))
        for member, da in members:
            val = float(da.values[lat_idx, lon_idx]) * scale
            if np.isnan(val):
                continue
            out.setdefault(member, []).append((valid, val))
    return out


def plot_ensemble_spread_single_run(
    site_name: str, model_id: str, field: str, out_path: Path
) -> None:
    """The "normal" meteogram-style view: newest usable run, x-axis is valid
    time, one line per member - more useful than the run-evolution version
    right now, while the eclipse is still far enough out that few runs have
    accumulated; the run-evolution view (plot_ensemble_spread) becomes the
    more useful one later as more runs pile up at the same fixed valid time."""
    all_sites = {s["name"]: s for s in load_sites()["sites"]}
    if site_name not in all_sites:
        raise ValueError(f"{site_name!r} not in config/placenames.json")
    site = all_sites[site_name]

    run_init = _pick_run_init(model_id)
    if run_init is None:
        raise ValueError(f"no usable run on disk for {model_id}")
    members = sample_member_series(model_id, run_init, field, site)
    if not members:
        raise ValueError(f"no cloud_{field} data found for {model_id} run {run_init}")

    fig, ax = plt.subplots(figsize=(11, 4.5))
    for m, points in sorted(members.items()):
        if not points:
            continue
        xs, ys = zip(*sorted(points), strict=True)
        if m <= 0:
            ax.plot(xs, ys, color="#C44E52", linewidth=1.6, zorder=5, label=f"member {m} (control)")
        else:
            ax.plot(xs, ys, color="#4C72B0", linewidth=0.5, alpha=0.3)

    by_valid: dict[datetime, list[float]] = {}
    for points in members.values():
        for valid, val in points:
            by_valid.setdefault(valid, []).append(val)
    mean_xs = sorted(by_valid)
    mean_ys = [sum(by_valid[v]) / len(by_valid[v]) for v in mean_xs]
    ax.plot(mean_xs, mean_ys, color="black", linewidth=2, zorder=6, label="ensemble mean")

    ax.set_ylim(0, 100)
    ax.set_ylabel(f"cloud_{field} %")
    ax.set_title(
        f"{site_name} - {model_id} cloud_{field} - {len(members)} members "
        f"(run {run_init.strftime('%Y-%m-%d %HZ')})"
    )
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--model", required=True, choices=["aifs_ens", "ecmwf_ens"])
    parser.add_argument("--field", default="low", choices=["low", "mid", "high", "total"])
    parser.add_argument(
        "--mode", default="single-run", choices=["single-run", "run-evolution"],
        help="single-run: newest run, x-axis=valid time (default). "
             "run-evolution: x-axis=run_init at one fixed valid time.",
    )
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    slug = args.site.replace(" ", "_").replace("/", "-")
    out_path = args.out_dir / f"{slug}_{args.model}_{args.field}_{args.mode}.png"
    if args.mode == "single-run":
        plot_ensemble_spread_single_run(args.site, args.model, args.field, out_path)
    else:
        plot_ensemble_spread(args.site, args.model, args.field, out_path)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
