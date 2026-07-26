"""Standalone, decoupled renderer: for every archived (model, run_init) on
disk, render every step x every structurally-supported field via
render_run() (see src/viz/tool1_renderer.py). Produces no tool-specific
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

from src.config import DATA_RAW
from src.viz.tool1_renderer import OUTPUT_DIR, render_run

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

    Mutates and writes `state` after every run, for
    backfill_progress.html to poll."""
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

    while queue:
        model_id, run_init = queue.pop(0)
        state["current_model"] = model_id
        state["current_run"] = _iso_z(run_init)
        _write_progress(state)

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
        state["current_run"] = None
        _write_progress(state)

        # Check EVERY model for new arrivals, not just the one just rendered -
        # the queue spans all models, so a run fetched for any of them should
        # be picked up at the next opportunity.
        new_pairs = []
        for m in models:
            fresh = [r for r in _archived_run_inits(m) if r not in seen[m]]
            if fresh:
                fresh.sort(reverse=True)
                seen[m].update(fresh)
                state["models"][m]["planned_runs"] += len(fresh)
                new_pairs.extend((m, r) for r in fresh)
        if new_pairs:
            # Newest across models first, then straight to the front of the
            # queue - ahead of the remaining (older) backlog.
            new_pairs.sort(key=lambda pair: pair[1], reverse=True)
            log.info(
                "%d new run(s) appeared mid-backfill, prioritizing: %s",
                len(new_pairs), [f"{m} {r.isoformat()}" for m, r in new_pairs],
            )
            queue = new_pairs + queue


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


if __name__ == "__main__":
    main()
