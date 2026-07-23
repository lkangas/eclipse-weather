"""T32 - Site ranking view.

Ranks config/sites.yaml's 7 named candidate sites by P(cloud_low < 20%) at
the eclipse valid time, using each model's LATEST available run_init found
in data/points.parquet. Outputs a horizontal bar chart (data/viz/site_ranking.svg)
and prints a plain-text ranked list to stdout.

Per CLAUDE.md's explicit "don't make it unnecessarily fancy" direction: this
is a functional prototype. matplotlib defaults, no color/style polish.

## Design choice: combining heterogeneous native/derived/total_only,
## deterministic/ensemble models into one P(clear) number per site

This is a genuine judgment call (CLAUDE.md doesn't fully specify it). The
approach taken here, "pooled samples":

    For a given site, gather every (model, member) row from each model's
    latest run_init at the valid time nearest the eclipse time, THAT HAS A
    cloud_low VALUE AT ALL. Each such row is treated as one independent
    Bernoulli sample of "is it clear" (cloud_low < threshold). A deterministic
    model contributes exactly one sample (its single member=-1 row). An
    ensemble model contributes one sample per member row present.
    P(clear) at that site = (# clear samples) / (# total samples), pooled
    across every contributing model.

Why this over the alternative ("first collapse each model to its own
P(clear), then average those per-model probabilities with equal weight per
MODEL"): pooling is simpler (one groupby, no two-stage weighting scheme) and
is explicitly the option CLAUDE.md's task brief flags as "better" for this
prototype. It also uses every physical draw (ensemble member) as equal
evidence, which is defensible - a 50-member ensemble genuinely represents 50
simulated realizations of the atmosphere, not one opinion.

The real tradeoff, and the reason this is flagged rather than treated as
obviously correct: pooling gives models with more members proportionally
more influence on the ranking. A single 50-member ensemble can outweigh five
one-vote deterministic models combined. That's why the printed report and
the chart both surface `n_models` (distinct contributing models) alongside
`n_samples` (total pooled samples) for every site - a ranking built from 3
models x lots of members reads very differently from one built from 3
different one-shot deterministic runs, even at an identical P(clear), and a
reader should be able to tell those apart at a glance. Sites with very few
contributing samples are flagged explicitly (see MIN_SAMPLES_WARN below).

## Which models can contribute at all

This is determined EMPIRICALLY from the real data every run (cloud_low
non-null), never hardcoded here - CLAUDE.md hard constraint #2 says
config/models.yaml is the single source of truth for model metadata, and
duplicating a "these models have native L/M/H" table into this module would
immediately go stale as models.yaml's `verify` items get resolved and new
models come online. What models.yaml suggests as of this writing (informational
only, NOT relied on for filtering logic):
  - Structurally CANNOT contribute (no cloud_low field exists at all):
    ecmwf_ens (classic IFS ENS: L/M/H "absent_in_open_data", total-only tcc),
    aemet_harmonie (AEMET has no L/M/H anywhere, "levels.present: false",
    provenance total_only).
  - CAN contribute (native or derived L/M/H documented): gefs_extended,
    gfs, aifs_single, aifs_ens, ecmwf_hres (derived via T22's RH method),
    icon_global, icon_eu, ukmo_global (provenance still `verify`), arpege_europe,
    arome_france.
The runtime report below re-derives this from whatever is actually in
data/points.parquet, which is the only trustworthy source - a model can be
absent from the data entirely (never fetched/extracted yet) even if
models.yaml says it should eventually have cloud_low.

## Valid-time selection

The task is to rank sites "at the eclipse valid time" (ECLIPSE_T, env-overridable,
see eclipse_t() below). Per CLAUDE.md's simulated-eclipse testing section, this
module may run against test/backfill data whose valid times are NOT actually
2026-08-12 - that's expected, not an error. So for each model, this picks
whichever valid timestamp in that model's latest-run_init slice is CLOSEST to
ECLIPSE_T (exact match if present), and reports the actual gap. No interpolation
between bracketing archive hours (15/18/21 UTC) - that's more machinery than a
"keep it simple" prototype needs; nearest-available is transparent and cheap.

## WNW-strip context (stretch goal)

The ranking itself uses only the 7 named sites (T24's WNW-strip points, named
"<site>_wnw<NN>km", are excluded from the ranked list). As optional extra
context, this module also computes, per site, the WORST (minimum) P(clear)
among that site's WNW-strip points at the same latest-run/nearest-valid
selection - printed alongside the main number, not plotted as its own bar,
to keep the chart itself a plain single-metric-per-site bar chart per the
"don't make it fancy" direction.
"""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from src.config import DATA_ROOT, POINTS_PARQUET, eclipse_config, load_models, load_sites

# NOTE on not importing src.fetchers.base.eclipse_t: that would force Python
# to run src/fetchers/__init__.py first, which eagerly imports the herbie
# fetcher -> cfgrib -> eccodes, and this dev box has no working eccodes
# install. Same avoidance pattern src/extract/base.py already uses for
# _init_dir_name - a small, deliberate re-implementation, not a copy-paste
# accident.
DEFAULT_ECLIPSE_T = "2026-08-12T18:30:00Z"

CLEAR_THRESHOLD_PCT_DEFAULT = 20.0
MIN_SAMPLES_WARN = 5  # flag a site's estimate if fewer pooled samples than this
WNW_SUFFIX_RE = re.compile(r"_wnw\d+km$")

OUTPUT_SVG_DEFAULT = DATA_ROOT / "viz" / "site_ranking.svg"


def eclipse_t() -> datetime:
    """Read ECLIPSE_T from the environment (falls back to config/models.yaml's
    eclipse.t, then DEFAULT_ECLIPSE_T). Never hardcode a date elsewhere."""
    import os

    raw = os.environ.get("ECLIPSE_T")
    if not raw:
        raw = eclipse_config().get("t", DEFAULT_ECLIPSE_T)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def load_points(path: Path) -> pl.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. Nothing to rank - the archiver hasn't produced "
            "data/points.parquet in this environment yet (see README.md's Status section)."
        )
    df = pl.read_parquet(path)
    if df.is_empty():
        raise ValueError(f"{path} exists but has zero rows - nothing to rank.")
    return df


def strip_parent_site(name: str) -> str:
    """'Luarca_wnw25km' -> 'Luarca'. Returns name unchanged if it isn't a strip point."""
    return WNW_SUFFIX_RE.sub("", name)


def latest_run_per_model(df: pl.DataFrame) -> pl.DataFrame:
    """Restrict to each model's own latest run_init (independently - different
    models publish on different schedules, there is no single shared 'latest')."""
    latest = df.group_by("model").agg(pl.col("run_init").max().alias("_latest_run_init"))
    return (
        df.join(latest, on="model", how="left")
        .filter(pl.col("run_init") == pl.col("_latest_run_init"))
        .drop("_latest_run_init")
    )


def nearest_valid_per_model(df: pl.DataFrame, target: datetime) -> pl.DataFrame:
    """Within each model's (already latest-run) rows, keep only the valid
    timestamp closest to `target`. Reports nothing itself - see
    valid_time_report() for the human-readable gap summary."""
    tagged = df.with_columns(
        (pl.col("valid") - pl.lit(target)).dt.total_seconds().abs().alias("_gap_s")
    )
    nearest = tagged.group_by("model").agg(pl.col("_gap_s").min().alias("_min_gap_s"))
    return (
        tagged.join(nearest, on="model", how="left")
        .filter(pl.col("_gap_s") == pl.col("_min_gap_s"))
        .drop("_min_gap_s")
    )


def valid_time_report(df: pl.DataFrame, target: datetime) -> pl.DataFrame:
    """Per model: which valid timestamp was actually used, and how far (hours)
    it sits from the eclipse valid time - the transparency CLAUDE.md's
    simulated-testing framing calls for when this isn't real Aug 12 data."""
    return (
        df.group_by("model")
        .agg(pl.col("valid").first().alias("valid_used"), pl.col("_gap_s").first())
        .with_columns((pl.col("_gap_s") / 3600.0).alias("gap_hours"))
        .drop("_gap_s")
        .sort("gap_hours")
    )


def model_contribution_report(
    named_latest_nearest: pl.DataFrame, all_model_names: list[str]
) -> tuple[pl.DataFrame, list[str], list[str]]:
    """Which models are actually usable for the ranking, determined purely from
    the real data (not from models.yaml - see module docstring). Returns
    (per-model row/field-availability table, contributing model names,
    excluded model names with a reason each)."""
    per_model = named_latest_nearest.group_by("model").agg(
        pl.len().alias("n_rows"),
        pl.col("cloud_low").is_not_null().sum().alias("n_with_cloud_low"),
        pl.col("provenance").unique().sort().alias("provenance_values"),
    )
    present_models = set(per_model["model"].to_list())
    contributing = sorted(
        row["model"]
        for row in per_model.filter(pl.col("n_with_cloud_low") > 0).iter_rows(named=True)
    )
    excluded_notes = []
    for name in sorted(all_model_names):
        if name not in present_models:
            excluded_notes.append(f"{name}: not present in data at all (never fetched/extracted)")
        elif name not in contributing:
            excluded_notes.append(f"{name}: present but cloud_low null for every row (no L/M/H)")
    return per_model.sort("model"), contributing, excluded_notes


def build_site_ranking(
    named_latest_nearest: pl.DataFrame, site_names: list[str], threshold_pct: float
) -> pl.DataFrame:
    """The pooled-sample P(cloud_low < threshold_pct) per site - see module
    docstring for the "why pooling" reasoning. Every one of the 7 named sites
    is always present in the output, even with zero eligible samples (shown
    as null p_clear), so a missing estimate is visible rather than silently
    dropped."""
    eligible = named_latest_nearest.filter(pl.col("cloud_low").is_not_null())
    stats = (
        eligible.group_by("site")
        .agg(
            pl.len().alias("n_samples"),
            (pl.col("cloud_low") < threshold_pct).sum().alias("n_clear"),
            pl.col("model").n_unique().alias("n_models"),
            pl.col("model").unique().sort().alias("models"),
            (pl.col("provenance") == "native").sum().alias("n_native"),
            (pl.col("provenance") == "derived").sum().alias("n_derived"),
        )
        .with_columns((pl.col("n_clear") / pl.col("n_samples")).alias("p_clear"))
    )
    base = pl.DataFrame({"site": site_names})
    ranking = base.join(stats, on="site", how="left").with_columns(
        pl.col("n_samples").fill_null(0),
        pl.col("n_clear").fill_null(0),
        pl.col("n_models").fill_null(0),
        pl.col("n_native").fill_null(0),
        pl.col("n_derived").fill_null(0),
    )
    # sort worst-to-best with "no data" (null p_clear) treated as worst of all,
    # so plotting it bottom-to-top via barh naturally puts the best site on top.
    return ranking.sort("p_clear", descending=False, nulls_last=False)


def build_wnw_worst_case(
    strip_latest_nearest: pl.DataFrame, threshold_pct: float
) -> dict[str, float]:
    """Per parent site: the minimum P(clear) among its WNW-sightline strip
    points (T24), pooled the same way as the main metric. Returns {} if the
    data has no strip points at all (e.g. this fixture / an early backfill
    that predates T24) - callers must handle that gracefully, not treat it
    as an error."""
    eligible = strip_latest_nearest.filter(pl.col("cloud_low").is_not_null())
    if eligible.is_empty():
        return {}
    eligible = eligible.with_columns(
        pl.col("site").map_elements(strip_parent_site, return_dtype=pl.String).alias("parent_site")
    )
    per_point = (
        eligible.group_by(["parent_site", "site"])
        .agg(
            pl.len().alias("n"),
            (pl.col("cloud_low") < threshold_pct).sum().alias("n_clear"),
        )
        .with_columns((pl.col("n_clear") / pl.col("n")).alias("p_clear"))
    )
    worst = per_point.group_by("parent_site").agg(
        pl.col("p_clear").min().alias("wnw_worst_p_clear")
    )
    parents = worst["parent_site"].to_list()
    worst_vals = worst["wnw_worst_p_clear"].to_list()
    return dict(zip(parents, worst_vals, strict=True))


def plot_ranking(
    ranking: pl.DataFrame,
    wnw_worst: dict[str, float],
    threshold_pct: float,
    target: datetime,
    max_gap_hours: float,
    out_path: Path,
) -> None:
    sites = ranking["site"].to_list()
    p_clear = ranking["p_clear"].to_list()
    n_samples = ranking["n_samples"].to_list()
    n_models = ranking["n_models"].to_list()

    heights = [0.0 if v is None else v * 100 for v in p_clear]
    colors = ["#999999" if v is None else "#4C72B0" for v in p_clear]

    fig, ax = plt.subplots(figsize=(8, 0.6 * len(sites) + 1.5))
    bars = ax.barh(sites, heights, color=colors)

    labels = []
    for i, site in enumerate(sites):
        if p_clear[i] is None:
            labels.append("no data")
        else:
            label = f"{heights[i]:.0f}%  (n={n_samples[i]}, {n_models[i]} models)"
            worst = wnw_worst.get(site)
            if worst is not None:
                label += f"  |  WNW worst {worst * 100:.0f}%"
            labels.append(label)
    ax.bar_label(bars, labels=labels, padding=3, fontsize=8)

    ax.set_xlabel(f"P(cloud_low < {threshold_pct:.0f}%)  [%]")
    ax.set_xlim(0, 100)
    title = f"Site ranking - P(clear) at eclipse valid time (T={target.isoformat()})"
    if max_gap_hours > 12:
        title += (
            f"\n(TEST DATA: nearest available valid time is up to "
            f"{max_gap_hours:.0f}h from T - not real Aug 12 data)"
        )
    ax.set_title(title, fontsize=10)
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # No explicit format= - matplotlib infers it from out_path's suffix, so
    # --out still does the right thing if ever pointed at a .png instead.
    fig.savefig(out_path)
    plt.close(fig)


def print_report(
    ranking: pl.DataFrame,
    wnw_worst: dict[str, float],
    contributing_models: list[str],
    excluded_notes: list[str],
    valid_report: pl.DataFrame,
    threshold_pct: float,
    target: datetime,
) -> None:
    print("=" * 78)
    print(f"T32 site ranking - P(cloud_low < {threshold_pct:.0f}%) at eclipse valid time")
    print(f"ECLIPSE_T = {target.isoformat()}")
    print("=" * 78)

    print(f"\nModels contributing cloud_low samples ({len(contributing_models)}):")
    for m in contributing_models:
        print(f"  - {m}")
    if excluded_notes:
        print(f"\nModels excluded from this ranking ({len(excluded_notes)}):")
        for note in excluded_notes:
            print(f"  - {note}")

    print("\nValid time actually used per contributing model (gap from ECLIPSE_T):")
    for row in valid_report.iter_rows(named=True):
        print(f"  {row['model']:<16} valid={row['valid_used']}  gap={row['gap_hours']:+.1f}h")

    print("\nRanking (best P(clear) first):")
    header = f"{'rank':<5}{'site':<12}{'P(clear)':<10}{'n_samples':<11}{'n_models':<10}{'models'}"
    print(header)
    print("-" * len(header))
    ordered_best_first = ranking.sort("p_clear", descending=True, nulls_last=True)
    for i, row in enumerate(ordered_best_first.iter_rows(named=True), start=1):
        if row["p_clear"] is None:
            pct = "no data"
        else:
            pct = f"{row['p_clear'] * 100:.0f}%"
        models_str = ", ".join(row["models"]) if row["models"] else "-"
        flag = ""
        if row["n_samples"] and row["n_samples"] < MIN_SAMPLES_WARN:
            flag = f"  <-- only {row['n_samples']} sample(s), weak estimate"
        wnw = wnw_worst.get(row["site"])
        wnw_str = f" (WNW worst {wnw * 100:.0f}%)" if wnw is not None else ""
        print(
            f"{i:<5}{row['site']:<12}{pct:<10}{row['n_samples']:<11}{row['n_models']:<10}"
            f"{models_str}{wnw_str}{flag}"
        )
    if not wnw_worst:
        print("\n(No WNW-strip points found in this data - worst-case sightline column omitted.)")


def run(points_path: Path, out_path: Path, threshold_pct: float) -> pl.DataFrame:
    df = load_points(points_path)
    target = eclipse_t()

    df = df.with_columns(pl.col("site").str.contains("_wnw").alias("is_strip"))
    latest = latest_run_per_model(df)
    nearest = nearest_valid_per_model(latest, target)

    named_site_names = [s["name"] for s in load_sites()["sites"]]
    named = nearest.filter(~pl.col("is_strip") & pl.col("site").is_in(named_site_names))
    strip = nearest.filter(pl.col("is_strip"))

    all_model_names = list(load_models()["models"].keys())
    per_model_report, contributing_models, excluded_notes = model_contribution_report(
        named, all_model_names
    )
    v_report = valid_time_report(named.filter(pl.col("model").is_in(contributing_models)), target)
    max_gap_hours = v_report["gap_hours"].abs().max() if not v_report.is_empty() else 0.0

    ranking = build_site_ranking(named, named_site_names, threshold_pct)
    wnw_worst = build_wnw_worst_case(strip, threshold_pct)

    print_report(
        ranking, wnw_worst, contributing_models, excluded_notes, v_report, threshold_pct, target
    )
    plot_ranking(ranking, wnw_worst, threshold_pct, target, max_gap_hours, out_path)
    print(f"\nChart written to {out_path}")

    return ranking


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--points",
        type=Path,
        default=POINTS_PARQUET,
        help=f"path to points.parquet (default: {POINTS_PARQUET})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUTPUT_SVG_DEFAULT,
        help=f"output SVG path (default: {OUTPUT_SVG_DEFAULT})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=CLEAR_THRESHOLD_PCT_DEFAULT,
        help="cloud_low %% threshold counted as 'clear' (default: 20)",
    )
    args = parser.parse_args(argv)
    run(args.points, args.out, args.threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
