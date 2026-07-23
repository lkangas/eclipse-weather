"""src/viz/run_evolution.py -- T31b: per-site run-evolution small multiples.

This is the "(b) Per-site trajectory small-multiples" half of TASKS.md's T31.
T31a's Iberia-map + totality-path layer is explicitly NOT part of this module
(the totality-path polygon is T33's job, scheduled for Aug 9-10 once real
eclipse-path data exists -- see TASKS.md).

For one FIXED valid time T, plots cloud% (y axis) against run_init (x axis --
the "how did the forecast evolve run-over-run" slider dimension) as small
multiples: one panel per (model, site) pair that has enough distinct
run_inits in data/points.parquet to show a real trajectory (>= MIN_RUN_INITS).
Three lines per panel (low/mid/high cloud%) where a model has native or
derived L/M/H; just one line (cloud_total) for total_only models (AEMET) or
for ecmwf_hres/ecmwf_ens's native-total-only rows.

Fixed valid time -- picked generically, not hardcoded
-------------------------------------------------------------------------
T31 calls for a fixed valid time T (config/models.yaml's eclipse.t, override-
able via the ECLIPSE_T env var per CLAUDE.md hard constraint -- never a
literal date in code). Real archived data doesn't reach 2026-08-12 yet (T16's
Open-Meteo backfill against the true eclipse date isn't built -- see
TASKS.md), so the valid times that actually exist in points.parquet right now
are whatever ECLIPSE_T override(s) were live when each model was fetch-tested
during T20-T24 (2026-07-21/22). That turned out to NOT even be a single date:
most models were tested against a 2026-07-25 target, but the two short-range
models (arome_france: 51h max reach, aemet_harmonie: 48h max reach) needed a
closer 2026-07-23 target to have any coverage at all, and gefs_extended's
earliest archived run (already reaching 35 days out, per models.yaml's
"ALREADY LIVE" note) was separately tested against the real 2026-08-12 date.

Rather than hardcode any of those dates, pick_fixed_valid_time() picks,
generically, whichever valid time actually present in points.parquet is
closest to eclipse.t/ECLIPSE_T AND has at least one (model, site) pair with
>= MIN_RUN_INITS distinct run_inits -- so it doesn't lock onto an isolated
single-run valid time (e.g. that lone 2026-08-12 gefs_extended row) and
produce an almost-empty grid. Once T16 lands real multi-day history against
the true eclipse date, this same logic picks 2026-08-12T18:00Z automatically
-- nothing here needs to change. The picked valid time and this whole
situation is printed/annotated on the figure so it's never mistaken for the
real eclipse moment.

Ensembles -- percentile band, not spaghetti
-------------------------------------------------------------------------
Ensemble members (ecmwf_ens, aifs_ens; gefs_extended is nominally an
ensemble in models.yaml but T20 only ever fetches its control member, so it
has exactly one distinct `member` value in the real archive and is plotted
as a single line automatically -- see is_ensemble's data-driven definition
below) are drawn as a 10th-90th percentile band + median line per run_init,
not full member spaghetti. Simpler to implement correctly and much less
visually noisy in a small-multiples grid -- documented here as the
deliberate choice the task brief asked for.

Provenance (CLAUDE.md hard constraint #3)
-------------------------------------------------------------------------
ecmwf_hres writes two PointRows per (site, valid, run_init): a
provenance="native" row with cloud_total only, and a provenance="derived" row
with cloud_low/mid/high only (see src/extract/ecmwf_extractor.py's module
docstring). This module's "levels" panels select purely on
cloud_low.is_not_null(), which for ecmwf_hres naturally selects only its
derived rows -- the native-total row is never touched by the L/M/H lines, so
nothing gets averaged across the two provenances. The panel's row label
flags "(derived L/M/H)" for any model where that's what's being shown.

Output: one consolidated grid figure, data/viz/run_evolution.svg. Rows are
models with at least one qualifying site (in models.yaml's own dict order),
columns are the 7 sites.yaml sites in their configured west-to-east order.
Cells with insufficient data are left blank with a small note rather than
omitted, so the grid stays rectangular and easy to scan.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from src.config import POINTS_PARQUET, REPO_ROOT, eclipse_config, load_models, load_sites

OUTPUT_DIR = REPO_ROOT / "data" / "viz"
OUTPUT_PATH = OUTPUT_DIR / "run_evolution.svg"

# A (model, site) panel needs at least this many distinct run_inits at the
# fixed valid time to be worth plotting as a trajectory.
MIN_RUN_INITS = 2

BAND_COLORS = {"low": "tab:blue", "mid": "tab:orange", "high": "tab:green"}
TOTAL_COLOR = "tab:red"


def _eclipse_t() -> datetime:
    """ECLIPSE_T from the environment, falling back to config/models.yaml's
    eclipse.t (CLAUDE.md: 'ECLIPSE_T = env var — never hardcode; UI must work
    with any T'). Reimplemented locally rather than importing
    src.fetchers.base.eclipse_t() -- importing anything under src.fetchers
    eagerly imports herbie -> cfgrib -> eccodes and crashes on a Windows box
    with no working ecCodes install (same reasoning documented in
    src/extract/open_meteo_extractor.py). This viz module only needs
    matplotlib/polars/pyyaml and should stay importable on plain Windows
    Python."""
    raw = os.environ.get("ECLIPSE_T") or eclipse_config()["t"]
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def load_site_points() -> pl.DataFrame:
    """points.parquet filtered to the 7 named sites.yaml sites only -- drops
    T24's WNW-strip points (e.g. 'Luarca_wnw50km'). Strip-based views are a
    future enhancement (per this task's brief), not part of this chart."""
    if not POINTS_PARQUET.exists():
        raise FileNotFoundError(
            f"{POINTS_PARQUET} does not exist -- run the archiver (src/scheduler/run.py) "
            "or otherwise populate it before plotting."
        )
    df = pl.read_parquet(POINTS_PARQUET)
    site_names = {s["name"] for s in load_sites()["sites"]}
    return df.filter(pl.col("site").is_in(site_names))


def pick_fixed_valid_time(df: pl.DataFrame) -> datetime:
    """The valid time to fix the whole figure on -- see module docstring."""
    target = _eclipse_t()
    valid_values = df.get_column("valid").unique().to_list()
    if not valid_values:
        raise ValueError("points.parquet has no rows for the 7 named sites -- nothing to plot")

    counts = df.group_by(["valid", "model", "site"]).agg(
        pl.col("run_init").n_unique().alias("n_runs")
    )
    eligible = (
        counts.filter(pl.col("n_runs") >= MIN_RUN_INITS).get_column("valid").unique().to_list()
    )
    pool = eligible or valid_values
    return min(pool, key=lambda v: abs((v - target).total_seconds()))


def _quantile_band(g: pl.DataFrame, col: str) -> pl.DataFrame:
    """Per run_init: p10/median/p90 of `col` across whatever members are
    present -- the ensemble panel's plot data."""
    return (
        g.group_by("run_init")
        .agg(
            pl.col(col).quantile(0.1).alias("p10"),
            pl.col(col).median().alias("p50"),
            pl.col(col).quantile(0.9).alias("p90"),
        )
        .sort("run_init")
        .drop_nulls()
    )


def _single_line(g: pl.DataFrame, col: str) -> pl.DataFrame:
    """Per run_init: `col`'s value -- the deterministic panel's plot data.
    group_by + mean is a defensive no-op here (exactly one non-null row per
    run_init is expected for a deterministic model); it is NOT averaging
    across provenances -- see module docstring's ecmwf_hres note, the
    cloud_low.is_not_null() filter upstream already isolates the right rows
    before this function ever sees them."""
    return (
        g.group_by("run_init").agg(pl.col(col).mean().alias(col)).sort("run_init").drop_nulls()
    )


def _plot_levels(ax: plt.Axes, g: pl.DataFrame, is_ensemble: bool) -> None:
    for band, color in BAND_COLORS.items():
        col = f"cloud_{band}"
        if g[col].null_count() == g.height:
            continue
        if is_ensemble:
            q = _quantile_band(g, col)
            if q.is_empty():
                continue
            x = q["run_init"].to_list()
            ax.plot(x, q["p50"].to_list(), color=color, marker="o", markersize=3, label=band)
            ax.fill_between(x, q["p10"].to_list(), q["p90"].to_list(), color=color, alpha=0.15)
        else:
            line = _single_line(g, col)
            if line.is_empty():
                continue
            ax.plot(
                line["run_init"].to_list(), line[col].to_list(),
                color=color, marker="o", markersize=3, label=band,
            )


def _plot_total(ax: plt.Axes, g: pl.DataFrame, is_ensemble: bool) -> None:
    if is_ensemble:
        q = _quantile_band(g, "cloud_total")
        if q.is_empty():
            return
        x = q["run_init"].to_list()
        ax.plot(x, q["p50"].to_list(), color=TOTAL_COLOR, marker="o", markersize=3, label="total")
        ax.fill_between(x, q["p10"].to_list(), q["p90"].to_list(), color=TOTAL_COLOR, alpha=0.15)
    else:
        line = _single_line(g, "cloud_total")
        if line.is_empty():
            return
        ax.plot(
            line["run_init"].to_list(), line["cloud_total"].to_list(),
            color=TOTAL_COLOR, marker="o", markersize=3, label="total",
        )


def build_panels(df: pl.DataFrame, valid_time: datetime) -> dict[tuple[str, str], dict]:
    """Every qualifying (model, site) panel at the fixed valid time: which
    kind of lines it gets ('levels' | 'total'), whether it's an ensemble
    (data-driven: >1 distinct `member` actually archived, not just
    models.yaml's nominal `kind`), and the rows to plot."""
    sub = df.filter(pl.col("valid") == valid_time)
    pairs = sub.select(["model", "site"]).unique().rows()

    panels: dict[tuple[str, str], dict] = {}
    for model, site in pairs:
        g = sub.filter((pl.col("model") == model) & (pl.col("site") == site))
        n_runs = g.get_column("run_init").n_unique()
        if n_runs < MIN_RUN_INITS:
            continue
        n_members = g.get_column("member").n_unique()
        has_levels = g.get_column("cloud_low").null_count() < g.height
        has_total = g.get_column("cloud_total").null_count() < g.height
        if has_levels:
            kind = "levels"
        elif has_total:
            kind = "total"
        else:
            continue
        panels[(model, site)] = {
            "rows": g,
            "kind": kind,
            "is_ensemble": n_members > 1,
            "n_runs": n_runs,
            "n_members": n_members,
            "provenance": set(g.get_column("provenance").unique().to_list()),
        }
    return panels


def make_figure(panels: dict[tuple[str, str], dict], valid_time: datetime) -> plt.Figure:
    sites_in_order = [s["name"] for s in load_sites()["sites"]]
    sites_with_data = [s for s in sites_in_order if any(site == s for _, site in panels)]
    if not sites_with_data:
        sites_with_data = sites_in_order

    models_in_order = list(load_models()["models"])
    models_with_data = [m for m in models_in_order if any(model == m for model, _ in panels)]

    # Per-model summary for row labels, built from every panel of that model
    # (not just whichever site lands in the leftmost column).
    model_meta: dict[str, dict] = {}
    for (model, _site), p in panels.items():
        meta = model_meta.setdefault(model, {"ensemble": False, "provenance": set()})
        meta["ensemble"] = meta["ensemble"] or p["is_ensemble"]
        meta["provenance"] |= p["provenance"]

    n_rows, n_cols = len(models_with_data), len(sites_with_data)
    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(2.4 * n_cols, 1.8 * n_rows), squeeze=False
    )

    for r, model in enumerate(models_with_data):
        for c, site in enumerate(sites_with_data):
            ax = axes[r][c]
            panel = panels.get((model, site))
            if panel is None:
                ax.text(
                    0.5, 0.5, "insufficient\ndata", ha="center", va="center",
                    fontsize=7, color="gray", transform=ax.transAxes,
                )
                ax.set_xticks([])
                ax.set_yticks([])
            else:
                g = panel["rows"]
                if panel["kind"] == "levels":
                    _plot_levels(ax, g, panel["is_ensemble"])
                else:
                    _plot_total(ax, g, panel["is_ensemble"])
                ax.set_ylim(-5, 105)
                ax.tick_params(axis="x", labelrotation=45, labelsize=6)
                ax.tick_params(axis="y", labelsize=6)
                if r == 0 and c == 0:
                    ax.legend(fontsize=6, loc="upper left")
            if r == 0:
                ax.set_title(site, fontsize=8)
            if c == 0:
                meta = model_meta[model]
                label = model
                if "derived" in meta["provenance"]:
                    label += "\n(derived L/M/H)"
                if meta["ensemble"]:
                    label += "\n(p10-90 band)"
                ax.set_ylabel(label, fontsize=7, rotation=0, ha="right", va="center")

    fig.suptitle(
        f"Cloud % run-evolution at fixed valid time {valid_time.isoformat()}\n"
        f"SIMULATED/test valid time (from T20-T24 archive testing) -- NOT the real "
        f"eclipse moment. config eclipse.t = {eclipse_config()['t']}",
        fontsize=10,
    )
    fig.tight_layout(rect=(0.07, 0, 1, 0.92))
    return fig


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_site_points()
    valid_time = pick_fixed_valid_time(df)
    panels = build_panels(df, valid_time)
    if not panels:
        raise SystemExit(
            f"No (model, site) pair has >= {MIN_RUN_INITS} distinct run_inits at "
            f"valid={valid_time.isoformat()} -- nothing to plot yet."
        )

    fig = make_figure(panels, valid_time)
    fig.savefig(OUTPUT_PATH)
    plt.close(fig)

    print(f"wrote {OUTPUT_PATH}")
    print(f"fixed valid time: {valid_time.isoformat()}")
    print(f"{len(panels)} qualifying (model, site) panels:")
    for (model, site), p in sorted(panels.items()):
        print(
            f"  {model:16s} {site:10s} kind={p['kind']:6s} n_runs={p['n_runs']} "
            f"n_members={p['n_members']} ensemble={p['is_ensemble']} "
            f"provenance={sorted(p['provenance'])}"
        )


if __name__ == "__main__":
    main()
