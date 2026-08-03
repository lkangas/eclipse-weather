"""Per-point time-series ("meteogram") prototype.

UGLY ON PURPOSE, per explicit direction: matplotlib defaults, no layout/color
polish, no design decisions locked in - this exists to give the user real
example output to react to before any of that is decided.

Two separate figures per place (config/placenames.json) - "<site>_cloud.png"
and "<site>_temp.png" - one row per model in each, over that model's own
valid-time range for one run. Cloud and temp are NEVER on the same graph,
not even as a twin y-axis: differently-scaled quantities get their own plot,
full stop (direct feedback after the first prototype tried twinx()).

Why this reads raw GRIB2 directly instead of data/points.parquet: points.parquet
(CLAUDE.md's fixed schema) has no temperature field at all - there is no
per-model surface_temp extraction pipeline yet, even though models.yaml's T37/
T45 research already nailed down exactly which param/package/level each model
uses. That pipeline is real future work (the "final renderer" this prototype
exists to preview, which needs raw files kept around long enough to run it -
see the production discard-after-render design in TASKS.md). This script
shortcuts that for cloud too, not just temp, so every line in a given row
comes from the same run/read path rather than mixing a possibly-stale
points.parquet extraction with a fresh raw read for the same model.

Reuses frame_renderer.py's already-tested per-model field readers
(_MODEL_READERS) - same (field, run_init, step, bbox) -> (lats, lons, values)
contract the map renderer uses - and just nearest-samples one gridpoint
instead of drawing the whole grid. Only covers the 10 models in
frame_renderer._TEMP_CAPABLE_READERS; aemet_harmonie (no temp, and its
archive is a color-ramp raster, not numeric) and the 4 Open-Meteo point-API
models (no grid to sample) are out of scope here.

Which of low/mid/high/total/temp a given model actually has is NOT looked up
from models.yaml - it is discovered empirically per model by calling the
reader and seeing what comes back non-None (same "trust the reader, not the
label" philosophy site_ranking.py's docstring already argues for), so this
never needs updating when a model's fields change.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from src.config import DATA_RAW, DATA_ROOT, eclipse_config, get_model, load_sites
from src.viz.frame_renderer import _MODEL_READERS, _TEMP_CAPABLE_READERS, _steps_for_run

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_ROOT / "viz" / "point_timeseries"
CANDIDATE_FIELDS = ["low", "mid", "high", "total", "temp"]
FIELD_COLORS = {"low": "#4C72B0", "mid": "#DD8452", "high": "#55A868", "total": "#000000"}

# Only models whose reader can produce temp at all (frame_renderer's own
# capability set) - cloud-only models would work for 4 of 5 fields here, but
# mixing "has temp" and "cloud only" rows into one figure is a later decision,
# not this prototype's to make.
_MODEL_IDS_ALL = sorted(name for name, fn in _MODEL_READERS.items() if fn in _TEMP_CAPABLE_READERS)


def _pick_run_init(
    model_id: str,
    lookback: int = 10,
    min_files: int = 5,
    min_file_bytes: int = 500_000,
    min_good_fraction: float = 0.8,
) -> datetime | None:
    """Newest run_init under data/raw/<model_id> that isn't near-empty.

    Guards against picking a run still mid-fetch, or one truncated by the
    2026-08-03 disk-full incident. An absolute min_files count was tried
    first and wasn't enough either: gfs's 2026073112 run has 90 of its 104
    files at exactly 0 bytes and only ~8 genuinely complete ones - comfortably
    clears "at least 5 good files" while being 87% dead, and every one of
    those dead steps then gets attempted (and fails) for every field in
    CANDIDATE_FIELDS, which is most of why an early version of this function
    made the whole overlay run take 5+ minutes instead of ~1. Requiring a
    FRACTION of files to be good-sized (not just a fixed count) catches a
    mostly-corrupted run regardless of how many files it happens to have;
    the fixed min_files floor stays too, so a run with only 2-3 files total
    (still mid-fetch) doesn't pass on fraction alone.
    """
    run_root = DATA_RAW / model_id
    if not run_root.is_dir():
        return None
    names = sorted(
        (d.name for d in run_root.iterdir() if d.is_dir() and d.name.isdigit()), reverse=True
    )
    for name in names[:lookback]:
        run_dir = run_root / name
        # Exclude cfgrib's .idx sidecars (created when a file is opened, not
        # when it's fetched - src/fetchers/base.py's raw_data_files() excludes
        # them for the same reason) and this project's own dot-marker files
        # (.extracted, .last_fetch_attempt, .last_render): both are always
        # small/near-empty even in a perfectly complete run, so counting them
        # as "data files" tanks the good-fraction of a genuinely fine run to
        # ~50% (measured on gfs's own known-good 2026073100).
        files = [
            f for f in run_dir.iterdir()
            if f.is_file() and not f.name.startswith(".") and f.suffix != ".idx"
        ]
        if not files:
            continue
        good = sum(1 for f in files if f.stat().st_size >= min_file_bytes)
        if good >= min_files and good / len(files) >= min_good_fraction:
            return datetime.strptime(name, "%Y%m%d%H").replace(tzinfo=UTC)
    return None


def pick_overlay_field(model_id: str, run_init: datetime, bbox: dict) -> str | None:
    """Which SINGLE cloud field to sample for this model in overlay mode:
    'low' if the model has it, else 'total' (ecmwf_hres/ecmwf_ens have no
    native low/mid/high at all), else None (nothing usable). Probes just the
    FIRST available step rather than sampling every field at every step -
    the overlay only ever plots one cloud line per model anyway, so there's
    no reason to pay for mid/high too (see sample_model_series's fields
    param, added after an 18-minute run turned out to be sampling 5 fields
    per step when only 1 was ever going to be drawn)."""
    reader = _MODEL_READERS[model_id]
    model_config = get_model(model_id)
    steps = _steps_for_run(model_id, model_config, run_init)
    for step in steps[:3]:
        try:
            if reader("low", run_init, step, bbox) is not None:
                return "low"
        except Exception:
            pass
        try:
            if reader("total", run_init, step, bbox) is not None:
                return "total"
        except Exception:
            pass
    return None


def sample_model_series(
    model_id: str, run_init: datetime, sites: list[dict], fields: list[str] = CANDIDATE_FIELDS
) -> dict[str, dict[str, list[tuple[datetime, float]]]]:
    """{site_name: {field: [(valid_dt, value), ...]}} for every field in
    `fields` (default: all of CANDIDATE_FIELDS) this model's reader actually
    returns data for, sampled at the nearest gridpoint to each site's (lat,
    lon). One reader call per (step, field) - simple over fast, see module
    docstring. Pass a narrower `fields` list (see pick_overlay_field) when
    only one or two fields will actually be used - sampling all 5 by default
    is fine for the per-model row charts, which draw all of them, but
    wasteful for a single-line-per-model overlay.

    Once ANY field raises for a given step, the rest of CANDIDATE_FIELDS are
    skipped for that same step rather than each independently hitting the
    same broken read: low/mid/high/total for a GFS-family model all live in
    ONE file (cloud_f{step}.grib2), so a corrupted step fails identically 4
    times over otherwise - and cfgrib's own failure path (try the .idx, fail,
    delete it, retry, fail again) is not cheap to repeat unnecessarily.
    _pick_run_init's good-fraction check already screens out the "mostly
    corrupted run" case; this handles the few individually-corrupted steps
    that can still slip through an overall-fine run. Imprecise for models
    where cloud and temp are genuinely separate files (a good temp step
    might get skipped because cloud failed first) - traded deliberately for
    speed in a prototype, not a correctness-critical path."""
    reader = _MODEL_READERS[model_id]
    model_config = get_model(model_id)
    steps = _steps_for_run(model_id, model_config, run_init)
    bbox = eclipse_config()["bbox"]
    out = {s["name"]: {f: [] for f in CANDIDATE_FIELDS} for s in sites}

    for step in steps:
        valid = run_init + timedelta(hours=step)
        for field in fields:
            try:
                result = reader(field, run_init, step, bbox)
            except Exception:
                log.warning("%s: reader failed for field=%s step=%s", model_id, field, step)
                break  # rest of this step's fields almost certainly share the same broken file
            if result is None:
                continue
            lats, lons, values = result
            if lats.size == 0 or lons.size == 0:
                continue
            for s in sites:
                lat_idx = int(np.argmin(np.abs(lats - s["lat"])))
                lon_idx = int(np.argmin(np.abs(lons - s["lon"])))
                val = float(values[lat_idx, lon_idx])
                if np.isnan(val):
                    continue
                out[s["name"]][field].append((valid, val))
    return out


def _target_dt() -> datetime:
    target = eclipse_config()["t"]
    return datetime.fromisoformat(target.replace("Z", "+00:00")).astimezone(UTC)


def _rows_with_field(
    site_name: str,
    per_model: dict[str, dict[str, dict[str, list[tuple[datetime, float]]]]],
    fields: tuple[str, ...],
) -> list[str]:
    return [m for m in per_model if any(per_model[m][site_name][f] for f in fields)]


def plot_cloud(
    site_name: str,
    per_model: dict[str, dict[str, dict[str, list[tuple[datetime, float]]]]],
    run_inits: dict[str, datetime | None],
    out_path: Path,
) -> None:
    """One row per model with any cloud field data - separate graph from
    temp entirely (not a twin axis - see feedback: never combine cloud and
    temp on the same plot, differently-scaled quantities get their own
    graphs)."""
    target_dt = _target_dt()
    rows = _rows_with_field(site_name, per_model, ("low", "mid", "high", "total"))
    if not rows:
        log.warning("%s: no cloud data from any model, skipping", site_name)
        return

    fig, axes = plt.subplots(len(rows), 1, figsize=(11, 2.2 * len(rows)), sharex=False)
    if len(rows) == 1:
        axes = [axes]

    for ax, model_id in zip(axes, rows, strict=True):
        series = per_model[model_id][site_name]
        for field in ("low", "mid", "high", "total"):
            points = series[field]
            if not points:
                continue
            xs, ys = zip(*sorted(points), strict=True)
            ax.plot(xs, ys, label=field, color=FIELD_COLORS[field], linewidth=1.2)
        ax.set_ylim(0, 100)
        ax.set_ylabel("cloud %")

        run_init = run_inits.get(model_id)
        run_str = run_init.strftime("%Y-%m-%d %HZ") if run_init else "?"
        ax.set_title(f"{model_id}  (run {run_str})", fontsize=9, loc="left")
        ax.axvline(target_dt, color="gray", linestyle=":", linewidth=1)
        ax.legend(loc="upper left", fontsize=7)

    fig.suptitle(f"{site_name} - cloud L/M/H/total")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_temp(
    site_name: str,
    per_model: dict[str, dict[str, dict[str, list[tuple[datetime, float]]]]],
    run_inits: dict[str, datetime | None],
    out_path: Path,
) -> None:
    """One row per model with temp data - own graph, own file, no cloud on it
    at all."""
    target_dt = _target_dt()
    rows = _rows_with_field(site_name, per_model, ("temp",))
    if not rows:
        log.warning("%s: no temp data from any model, skipping", site_name)
        return

    fig, axes = plt.subplots(len(rows), 1, figsize=(11, 2.0 * len(rows)), sharex=False)
    if len(rows) == 1:
        axes = [axes]

    for ax, model_id in zip(axes, rows, strict=True):
        xs, ys = zip(*sorted(per_model[model_id][site_name]["temp"]), strict=True)
        ax.plot(xs, ys, color="#C44E52", linewidth=1.4)
        ax.set_ylabel("temp C")

        run_init = run_inits.get(model_id)
        run_str = run_init.strftime("%Y-%m-%d %HZ") if run_init else "?"
        ax.set_title(f"{model_id}  (run {run_str})", fontsize=9, loc="left")
        ax.axvline(target_dt, color="gray", linestyle=":", linewidth=1)

    fig.suptitle(f"{site_name} - temperature")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_cloud_overlay(
    site_name: str,
    per_model: dict[str, dict[str, dict[str, list[tuple[datetime, float]]]]],
    run_inits: dict[str, datetime | None],
    out_path: Path,
) -> None:
    """All models, ONE graph, one line each - cloud_low where a model has it,
    else cloud_total (ecmwf_hres/ecmwf_ens have no native low/mid/high at
    all, see frame_renderer.py) - picked per model empirically, same "trust
    the reader" approach as everywhere else here. Cloud only - see
    plot_temp_overlay for temp, never combined on one graph."""
    target_dt = _target_dt()
    fig, ax = plt.subplots(figsize=(12, 6))
    plotted = 0
    for model_id, series_by_site in per_model.items():
        series = series_by_site[site_name]
        field = "low" if series["low"] else ("total" if series["total"] else None)
        if field is None:
            continue
        xs, ys = zip(*sorted(series[field]), strict=True)
        run_init = run_inits.get(model_id)
        run_str = run_init.strftime("%Y-%m-%d %HZ") if run_init else "?"
        ax.plot(xs, ys, linewidth=1.2, label=f"{model_id} ({field}, run {run_str})")
        plotted += 1
    if not plotted:
        log.warning("%s: no cloud data from any model, skipping overlay", site_name)
        plt.close(fig)
        return

    ax.set_ylim(0, 100)
    ax.set_ylabel("cloud %")
    ax.axvline(target_dt, color="gray", linestyle=":", linewidth=1)
    ax.set_title(f"{site_name} - all models - cloud_low where available, else cloud_total")
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def plot_temp_overlay(
    site_name: str,
    per_model: dict[str, dict[str, dict[str, list[tuple[datetime, float]]]]],
    run_inits: dict[str, datetime | None],
    out_path: Path,
) -> None:
    """All models, ONE graph, one temp line each - own graph, own file, no
    cloud on it at all (see feedback: never combine cloud and temp)."""
    target_dt = _target_dt()
    fig, ax = plt.subplots(figsize=(12, 5))
    plotted = 0
    for model_id, series_by_site in per_model.items():
        points = series_by_site[site_name]["temp"]
        if not points:
            continue
        xs, ys = zip(*sorted(points), strict=True)
        run_init = run_inits.get(model_id)
        run_str = run_init.strftime("%Y-%m-%d %HZ") if run_init else "?"
        ax.plot(xs, ys, linewidth=1.2, label=f"{model_id} (run {run_str})")
        plotted += 1
    if not plotted:
        log.warning("%s: no temp data from any model, skipping overlay", site_name)
        plt.close(fig)
        return

    ax.set_ylabel("temp C")
    ax.axvline(target_dt, color="gray", linestyle=":", linewidth=1)
    ax.set_title(f"{site_name} - all models - temperature")
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def run(site_names: list[str], model_ids: list[str], out_dir: Path, overlay: bool = False) -> None:
    all_sites = {s["name"]: s for s in load_sites()["sites"]}
    missing = [n for n in site_names if n not in all_sites]
    if missing:
        raise ValueError(f"not in config/placenames.json: {missing}")
    sites = [all_sites[n] for n in site_names]

    per_model = {}
    run_inits = {}
    for model_id in model_ids:
        run_init = _pick_run_init(model_id)
        run_inits[model_id] = run_init
        if run_init is None:
            print(f"{model_id}: no usable run on disk, skipping")
            per_model[model_id] = {
                s["name"]: {f: [] for f in CANDIDATE_FIELDS} for s in sites
            }
            continue
        if overlay:
            bbox = eclipse_config()["bbox"]
            field = pick_overlay_field(model_id, run_init, bbox)
            if field is None:
                print(f"{model_id}: no usable cloud field, skipping")
                per_model[model_id] = {s["name"]: {f: [] for f in CANDIDATE_FIELDS} for s in sites}
                continue
            print(f"{model_id}: sampling run {run_init.isoformat()} (field={field}+temp only) ...")
            per_model[model_id] = sample_model_series(model_id, run_init, sites, fields=[field, "temp"])
        else:
            print(f"{model_id}: sampling run {run_init.isoformat()} ...")
            per_model[model_id] = sample_model_series(model_id, run_init, sites)

    for s in sites:
        slug = s["name"].replace(" ", "_").replace("/", "-")
        if overlay:
            cloud_path = out_dir / f"{slug}_cloud_overlay.png"
            temp_path = out_dir / f"{slug}_temp_overlay.png"
            plot_cloud_overlay(s["name"], per_model, run_inits, cloud_path)
            plot_temp_overlay(s["name"], per_model, run_inits, temp_path)
        else:
            cloud_path = out_dir / f"{slug}_cloud.png"
            temp_path = out_dir / f"{slug}_temp.png"
            plot_cloud(s["name"], per_model, run_inits, cloud_path)
            plot_temp(s["name"], per_model, run_inits, temp_path)
        print(f"wrote {cloud_path}")
        print(f"wrote {temp_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sites", nargs="+", required=True, help="place name(s) from config/placenames.json")
    parser.add_argument("--models", nargs="+", default=_MODEL_IDS_ALL, help="model id(s), default: all temp-capable")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--overlay", action="store_true",
        help="one graph with one line per model, instead of one row per model",
    )
    args = parser.parse_args(argv)
    run(args.sites, args.models, args.out_dir, overlay=args.overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
