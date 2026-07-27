"""Standalone, decoupled renderer: for every archived (model, run_init) on
disk, render every step x every structurally-supported field via
render_run() (see src/viz/frame_renderer.py). Produces no tool-specific
manifest.json - just images on disk, in the same OUTPUT_DIR/model/field/
run_step.png convention render_frame() has always used - which Tool 1/2/3's
own manifest scripts can then scan and reference however each one needs.

This is the reusable rendering entry point the future production
fetch->render->delete pipeline will call once right after a fresh fetch
(then delete raw data once satisfied); here it's driven by a loop over
whatever's already archived, newest-first per model, so the freshest data
is available soonest during what can be a long-running backfill.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.render_backfill                            # every model, every archived run
    .venv/bin/python -m scripts.render_backfill --model gfs                # just one model
    .venv/bin/python -m scripts.render_backfill --model gfs --max-runs 2   # its 2 newest runs only (smoke test)
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime

from src.config import DATA_RAW, get_model
from src.fetchers.base import format_init_dir, full_range_steps, raw_data_files
from src.scheduler.run import regenerate_manifests
from src.viz.frame_renderer import OUTPUT_DIR, render_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("render_backfill")

MODELS = [
    "gfs", "gefs_extended", "arome_france", "arpege_europe", "ecmwf_hres",
    "ecmwf_ens", "aifs_single", "aifs_ens", "icon_eu", "icon_global",
]

# Live status for backfill_progress.html (served alongside the tool pages by
# the same static server - see scripts/serve_frames.py's own no-cache list,
# which must include this filename or the browser won't see updates).
PROGRESS_PATH = OUTPUT_DIR / "backfill_progress.json"


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _write_progress(state: dict) -> None:
    state["updated_at"] = _iso_z(datetime.now(UTC))
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_run_init(dirname: str) -> datetime | None:
    try:
        return datetime.strptime(dirname, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _archived_run_inits(model_id: str) -> list[datetime]:
    """Every archived run_init for this model, NEWEST FIRST - the freshest
    run should be available soonest during a long backfill (same
    rationale as generate_tool2_manifest.py's own reordering)."""
    d = DATA_RAW / model_id
    if not d.exists():
        return []
    run_inits = []
    for p in sorted(d.iterdir()):
        if not p.is_dir() or not any(p.iterdir()):
            continue  # skip stray/empty dirs (T35/T36 lesson - real test artifacts do turn up)
        run_init = _parse_run_init(p.name)
        if run_init is not None:
            run_inits.append(run_init)
    run_inits.sort(reverse=True)
    return run_inits


def _rendered_step_count(model_id: str) -> dict[str, int]:
    """{run_init_dirname: number of distinct steps with at least one frame PNG}
    for one model, from a single pass over its frame directories.

    Exact, because render_frame() writes NO file for a step/field with no data
    (see its docstring: "a frame exists on disk" == "has real data"), so a
    present PNG always means a rendered step. One pass per model rather than
    a glob per candidate run - this is called after every rendered run.
    """
    counts: dict[str, set[str]] = {}
    model_dir = OUTPUT_DIR / model_id
    if not model_dir.exists():
        return {}
    for field_dir in model_dir.iterdir():
        if not field_dir.is_dir():
            continue
        for png in field_dir.glob("*.png"):
            run_dirname, _, step = png.stem.rpartition("_")
            if run_dirname:
                counts.setdefault(run_dirname, set()).add(step)
    return {k: len(v) for k, v in counts.items()}


def _interleave_by_rank(per_model: dict[str, list[datetime]], models: list[str]) -> list[tuple[str, datetime]]:
    """Flatten {model: [newest, ..., oldest]} into one queue ordered by RANK,
    not by model: every model's newest run first, then every model's
    2nd-newest, and so on.

    This is the whole point of "newest first" - the goal is that all 10
    models have usable recent data quickly (so the tools are worth looking
    at) while history fills in behind. Rendering model-by-model instead
    would spend hours on one model's full 3-week backlog before the next
    model showed anything at all.

    Rank rather than a global sort on run_init, deliberately: models publish
    at wildly different cadences (arome_france 8 cycles/day vs ecmwf_hres 4),
    so a straight chronological sort would let the high-cadence models
    monopolise the whole recent window before a slower model's newest run
    came up."""
    queue: list[tuple[str, datetime]] = []
    for rank in range(max((len(v) for v in per_model.values()), default=0)):
        for model_id in models:
            runs = per_model[model_id]
            if rank < len(runs):
                queue.append((model_id, runs[rank]))
    return queue


def _run_backfill(models: list[str], max_runs: int | None, state: dict) -> None:
    """Render one rank-interleaved queue across ALL models (see
    _interleave_by_rank), re-checking disk after every run for anything
    fetched WHILE we were busy - a long backfill runs for hours and the
    scheduler keeps fetching the whole time, so a freshly-fetched run should
    jump the queue rather than wait behind the entire remaining backlog.

    That recheck covers two different kinds of "new data", not just one:
    brand-new run_inits, AND runs already rendered in this invocation that
    have since GAINED STEPS. The latter used to be invisible - the recheck
    only looked for run_inits it had never seen - so a run whose tail arrives
    late (NOAA publishes gefs_extended's 385-840h range ~25-27h after init,
    long after the run's directory first appeared) stayed rendered at
    whatever it held on first visit until a human re-ran the whole backfill.

    Mutates and writes `state` after every run, for
    backfill_progress.html to poll."""
    model_configs = {m: get_model(m) for m in models}
    per_model = {}
    for model_id in models:
        all_archived = _archived_run_inits(model_id)
        per_model[model_id] = all_archived[:max_runs] if max_runs is not None else all_archived
        state["models"][model_id] = {
            "planned_runs": len(per_model[model_id]), "completed": [],
        }
        log.info("%s: %d archived run(s) to render", model_id, len(per_model[model_id]))

    queue = _interleave_by_rank(per_model, models)
    # seen must cover EVERY run already on disk per model, not just the
    # (possibly --max-runs-sliced) subset queued: the recheck below compares
    # against the full unsliced _archived_run_inits() result, so seeding this
    # from the queue alone made every run excluded by --max-runs look "newly
    # appeared" - turning a --max-runs 1 smoke test into a full backfill.
    seen = {m: set(_archived_run_inits(m)) for m in models}

    # {(model, run_init): number of raw data files that existed when we last
    # rendered it}. Only runs this invocation actually rendered are ever keys,
    # which is what keeps the revisit check below inside --max-runs: a run the
    # slice excluded is never rendered, so it is never a revisit candidate
    # either (unlike `seen`, which must span the FULL unsliced list).
    rendered_raw_counts: dict[tuple[str, datetime], int] = {}

    while queue:
        model_id, run_init = queue.pop(0)
        state["current_model"] = model_id
        state["current_run"] = _iso_z(run_init)
        _write_progress(state)

        # Sampled BEFORE the render: a run gaining files while we render it
        # must still look "grown" afterwards, or those steps wait for the next
        # arrival to trigger a revisit.
        raw_count_before = len(raw_data_files(model_id, run_init))
        result = render_run(model_id, run_init)
        n_steps = len(result)
        n_with_data = sum(1 for fields in result.values() if any(fields.values()))
        log.info(
            "%s %s: rendered %d step(s), %d with real data",
            model_id, run_init.isoformat(), n_steps, n_with_data,
        )
        state["models"][model_id]["completed"].append({
            "run_init": _iso_z(run_init),
            "steps": n_steps,
            "steps_with_data": n_with_data,
            "finished_at": _iso_z(datetime.now(UTC)),
        })
        rendered_raw_counts[(model_id, run_init)] = raw_count_before
        state["current_run"] = None
        _write_progress(state)

        # Check EVERY model for new arrivals, not just the one just rendered -
        # the queue spans all models, so a run fetched for any of them should
        # be picked up at the next opportunity.
        new_pairs = []
        revisit_pairs = []
        for m in models:
            archived = _archived_run_inits(m)
            fresh = [r for r in archived if r not in seen[m]]
            if fresh:
                fresh.sort(reverse=True)
                seen[m].update(fresh)
                state["models"][m]["planned_runs"] += len(fresh)
                new_pairs.extend((m, r) for r in fresh)

            # Already-rendered runs that have since gained steps. TWO
            # conditions, both required, cheapest first:
            #   1. the run's raw directory has actually grown since we
            #      rendered it, and
            #   2. it is still short of the steps its cycle is supposed to
            #      publish (full_range_steps), so there is something left to
            #      render at all.
            # (1) is what prevents a permanent re-render loop: (2) alone is
            # true for almost every run forever, because plenty of steps
            # legitimately never produce a frame (arome_france's group files
            # start at +1h, and any step whose reader finds no data writes no
            # PNG by design). raw_data_files() excludes the .idx sidecars
            # cfgrib drops while READING a run, or rendering would itself look
            # like growth and re-trigger endlessly.
            step_counts = None
            for r in archived:
                key = (m, r)
                if key not in rendered_raw_counts:
                    continue
                if len(raw_data_files(m, r)) <= rendered_raw_counts[key]:
                    continue
                if step_counts is None:
                    step_counts = _rendered_step_count(m)
                expected = len(full_range_steps(model_configs[m], r))
                if step_counts.get(format_init_dir(r), 0) >= expected:
                    continue
                # Dropped from the map while queued so it cannot be enqueued
                # twice; re-recorded when it is rendered again.
                del rendered_raw_counts[key]
                state["models"][m]["planned_runs"] += 1
                revisit_pairs.append(key)

        if new_pairs or revisit_pairs:
            # Newest across models first, then straight to the front of the
            # queue - ahead of the remaining (older) backlog. Brand-new runs
            # lead: a run being revisited has already been rendered once, so
            # it is the less urgent of the two.
            new_pairs.sort(key=lambda pair: pair[1], reverse=True)
            revisit_pairs.sort(key=lambda pair: pair[1], reverse=True)
            if new_pairs:
                log.info(
                    "%d new run(s) appeared mid-backfill, prioritizing: %s",
                    len(new_pairs), [f"{m} {r.isoformat()}" for m, r in new_pairs],
                )
            if revisit_pairs:
                log.info(
                    "%d run(s) gained steps since being rendered, re-queueing: %s",
                    len(revisit_pairs), [f"{m} {r.isoformat()}" for m, r in revisit_pairs],
                )
            queue = new_pairs + revisit_pairs + queue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, help="only render this one model (default: all)")
    parser.add_argument(
        "--max-runs", type=int,
        help="only render this many of the newest archived runs per model (default: all archived runs)",
    )
    args = parser.parse_args()

    models = [args.model] if args.model else MODELS
    state = {
        "started_at": _iso_z(datetime.now(UTC)),
        "args": {"model": args.model, "max_runs": args.max_runs},
        # Every invocation writes the SAME global PROGRESS_PATH, so a targeted
        # run (--model X --max-runs 2) overwrites the view of a full backfill
        # and the progress page then reports "10% done" for what is really a
        # complete 2-run job. Recording the scope here at least lets a consumer
        # tell the two apart instead of silently misreading a partial run as
        # the whole archive; backfill_progress.html has to opt into showing it.
        "scope": "full" if (args.model is None and args.max_runs is None) else "partial",
        "current_model": None,
        "current_run": None,
        "models": {},
        "done": False,
    }
    _write_progress(state)

    _run_backfill(models, args.max_runs, state)

    state["current_model"] = None
    state["done"] = True
    _write_progress(state)
    log.info("backfill done")

    # The frames are worthless to the tools until the manifests describe them -
    # all three are pure disk scans (~12s for the set), so there is no reason
    # to leave this as a step a human has to remember after every backfill.
    # force=True: this is the end of the job, the rate limiter must not skip it.
    log.info("regenerating tool manifests")
    regenerate_manifests(force=True)


if __name__ == "__main__":
    main()
