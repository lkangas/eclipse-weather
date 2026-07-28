"""Bounding peak raw disk during a fetch, without rewriting any fetcher.

The problem (CLAUDE.md's measured disk-footprint note): a single in-flight
aifs_ens run is ~16 GB of raw GRIB before anything can be deleted. Even a
perfect delete-after-render pipeline that works a whole run at a time needs
16 GB of headroom on a box that may not have it. Peak footprint, not steady
state, is the binding constraint.

The fix is to fetch a run as a SEQUENCE of forecast-hour windows, rendering
and reclaiming after each, so peak raw is one window rather than one run.
Measured on the real archive 2026-07-27: aifs_ens writes one ~0.30 GB file
per step, so a 6-hour window (its own step cadence) is exactly one file and
caps peak raw for the worst model at ~0.3 GB instead of ~16 GB - a 53x
reduction, achieved without touching a single fetcher's download loop.

HOW, without a fetcher rewrite: every step-driven fetcher derives the steps
it will download from full_range_steps(model_config, run_init), and
model_config is just a dict the caller passes in. Narrowing that dict's
per-cycle max forecast length narrows the step list the fetcher walks. The
lower end of the window needs no support at all, because every fetcher is
already per-file idempotent - steps fetched by an earlier window are skipped
by a stat(), and steps already reclaimed are skipped via the tombstone (see
src/fetchers/base.raw_file_present).

This is a narrowing of a value models.yaml already defines, computed from
models.yaml, and never written back - models.yaml stays the single source of
truth (CLAUDE.md hard constraint #2).

WHICH FETCHERS HONOUR IT: the ones that iterate full_range_steps() -
"herbie" (gfs, gefs_extended), "ecmwf-opendata" (ecmwf_hres/ens,
aifs_single/ens) and "http_bz2" (icon_eu, icon_global). Those are also
precisely the heavy ones. The rest cannot be windowed and do not need to be:
"http_grib" (arome_france, arpege_europe) downloads Meteo-France's fixed
group files, so its granularity is already a ~40 MB group file and a whole
run is only a few hundred MB; "geotiff" (aemet_harmonie) and
"open_meteo_json" fetch one small bundle/JSON per run. peak_raw_bytes()
below reports the real bound for each.
"""

from __future__ import annotations

from datetime import datetime

from src.fetchers.base import full_range_steps

# Fetch kinds whose download loop is driven by full_range_steps() and can
# therefore be windowed by narrowing the per-cycle cap. Keyed by models.yaml's
# own `fetch:` value, so adding a model needs no change here - only adding a
# new FETCHER would.
CHUNKABLE_FETCH_KINDS = frozenset({"herbie", "ecmwf-opendata", "http_bz2"})


def is_chunkable(model_config: dict) -> bool:
    return model_config.get("fetch") in CHUNKABLE_FETCH_KINDS


def narrow_config(model_config: dict, run_init: datetime, cap_hours: int) -> dict:
    """A copy of model_config whose cycle for this run_init reaches no
    further than cap_hours. Everything else - steps cadence, source, params -
    is passed through untouched."""
    cycles = dict(model_config.get("cycles") or {})
    key = f"{run_init.hour:02d}"
    if key in cycles:
        cycles[key] = min(int(cycles[key]), int(cap_hours))
    else:
        cycles[key] = int(cap_hours)
    return {**model_config, "cycles": cycles}


def chunk_caps(model_config: dict, run_init: datetime, chunk_hours: int) -> list[int]:
    """Ascending window upper bounds covering everything this run publishes.

    Each cap is a real published step, so a window always adds at least one
    step and the last window reaches the run's full range (as capped by
    data_horizon inside full_range_steps).
    """
    steps = full_range_steps(model_config, run_init)
    if not steps:
        return []
    if not is_chunkable(model_config) or chunk_hours <= 0:
        return [steps[-1]]

    caps: list[int] = []
    current_bucket: int | None = None
    for step in steps:
        bucket = step // chunk_hours
        if bucket != current_bucket:
            caps.append(step)
            current_bucket = bucket
        else:
            caps[-1] = step  # extend the open window to this step
    return caps


def steps_in_chunk(
    model_config: dict, run_init: datetime, cap: int, previous_cap: int | None
) -> list[int]:
    """The steps a window [previous_cap+, cap] newly asks for - what the
    pipeline renders after that window's fetch returns."""
    steps = full_range_steps(narrow_config(model_config, run_init, cap), run_init)
    if previous_cap is None:
        return steps
    return [s for s in steps if s > previous_cap]


def measured_run_bytes(model_id: str) -> int:
    """Total raw bytes of the largest archived run of this model on this box,
    or 0 if there is none. Used for models whose raw carries no step number
    (open_meteo_json's forecast.json, aemet's GeoTIFF bundle), where a
    per-step figure is meaningless."""
    from src import config
    from src.pipeline import raw_layout

    model_dir = config.DATA_RAW / model_id
    if not model_dir.is_dir():
        return 0
    best = 0
    # All run dirs, not just the newest few: a model can have several empty
    # run directories at the head (a fetch that produced nothing yet), and
    # the figure wanted here is the largest real run this box has seen.
    for run_dir in sorted(model_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        try:
            total = sum(
                p.stat().st_size for p in run_dir.iterdir()
                if p.is_file() and not raw_layout.is_marker(p.name)
            )
        except OSError:
            continue
        best = max(best, total)
    return best


def bytes_per_step(model_id: str, fallback: int) -> int:
    """Measured average bytes per step for this model, from whatever runs are
    already archived on this box; `fallback` when there is nothing to measure
    (a fresh production box). Used only for the pre-fetch disk guard, never
    for a deletion decision."""
    from datetime import UTC

    from src import config
    from src.config import get_model
    from src.pipeline import raw_layout

    model_dir = config.DATA_RAW / model_id
    if not model_dir.is_dir():
        return fallback
    try:
        model_config = get_model(model_id)
    except KeyError:
        return fallback

    for run_dir in sorted(model_dir.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        try:
            run_init = datetime.strptime(run_dir.name, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        names = [p.name for p in run_dir.iterdir() if p.is_file()]
        total = sum(
            p.stat().st_size
            for p in run_dir.iterdir()
            if p.is_file() and not raw_layout.is_marker(p.name)
        )
        # Files per step varies per model (ecmwf_hres writes tcc_ + pl_ per
        # step, icon one file per param), so divide by DISTINCT steps present,
        # not by file count.
        distinct = len(raw_layout.group_files_by_step(model_id, model_config, run_init, names))
        if total > 0 and distinct:
            return total // distinct
    return fallback


def peak_raw_bytes(
    model_id: str, model_config: dict, run_init: datetime, chunk_hours: int, fallback: int
) -> int:
    """Worst-case raw on disk for one in-flight run of this model.

    THE WHOLE RUN, even for a chunkable fetcher. This used to return one
    window's worth, describing an intent the orchestrator does not implement:
    reclaim is not run after each chunk, it is run when the pre-fetch headroom
    check says the NEXT window would not fit (see orchestrator.process_run).
    Until then every fetched window stays on disk. Measured on the live VPS
    2026-07-28: aifs_ens reached 17 GB across 61 chunks while this function
    reported 0.26 GB - a 65x under-report, on the one model big enough to fill
    the disk, in the number the rollout tells you to check the floor against.

    What chunking actually bounds is the INCREMENT between headroom checks -
    how much can be committed before the pipeline gets another chance to stop -
    which print_sizing() now reports in its own column. The disk floor, not the
    window size, is what keeps this box safe.
    """
    from src.pipeline import fields as field_deps
    from src.pipeline import verify

    per_step = bytes_per_step(model_id, fallback)
    caps = chunk_caps(model_config, run_init, chunk_hours)
    if not caps:
        return 0
    carry = (
        field_deps.max_lookback(verify.expected_fields(model_id))
        if verify.is_renderable(model_id)
        else 0
    )
    # Whole run either way - see the docstring. Prefer a real measured run
    # size where one exists; a bytes-per-step extrapolation is meaningless for
    # the models whose files carry no step number at all (a single JSON, a
    # GeoTIFF bundle), and merely second-best for the others.
    measured = measured_run_bytes(model_id)
    if measured:
        return measured
    whole = per_step * len(full_range_steps(model_config, run_init))
    if not is_chunkable(model_config):
        return whole
    del carry  # only meaningful for the old one-window answer; a whole run
    # already contains every lookback input a differenced field could want.
    return whole


def window_increment_bytes(
    model_id: str, model_config: dict, run_init: datetime, chunk_hours: int, fallback: int
) -> int:
    """Raw committed by ONE window - what chunking actually bounds.

    The pipeline checks headroom before each window and reclaims if the next
    one would not fit, so this is the most that can be added between two
    chances to stop. Includes the lookback steps a differenced field keeps
    alive across the window boundary (src/pipeline/fields.py)."""
    from src.pipeline import fields as field_deps
    from src.pipeline import verify

    per_step = bytes_per_step(model_id, fallback)
    caps = chunk_caps(model_config, run_init, chunk_hours)
    if not caps or not is_chunkable(model_config):
        return 0
    carry = (
        field_deps.max_lookback(verify.expected_fields(model_id))
        if verify.is_renderable(model_id)
        else 0
    )
    widest, previous = 0, None
    for cap in caps:
        widest = max(widest, len(steps_in_chunk(model_config, run_init, cap, previous)))
        previous = cap
    return per_step * (widest + carry)
