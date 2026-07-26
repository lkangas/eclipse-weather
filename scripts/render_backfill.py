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


def _render_model(model_id: str, max_runs: int | None, state: dict) -> None:
    """Work through model_id's backlog as a dynamic queue, not a fixed list -
    after every render, re-check disk for a run fetched WHILE we were busy
    (this can be a long-running backfill; the scheduler keeps fetching the
    whole time) and jump straight to anything new before continuing down
    the old backlog. Without this, a freshly-fetched run would sit waiting
    behind whatever history was still left to render, possibly for a long
    time on a slow model.

    Mutates and writes `state` (see main()'s own shape) after every run
    completes, for backfill_progress.html to poll."""
    all_archived = _archived_run_inits(model_id)
    initial_run_inits = all_archived[:max_runs] if max_runs is not None else all_archived
    log.info("%s: %d archived run(s) to render", model_id, len(initial_run_inits))

    model_state = {"planned_runs": len(initial_run_inits), "completed": []}
    state["models"][model_id] = model_state
    state["current_model"] = model_id

    queue = list(initial_run_inits)
    # seen must cover EVERY run already on disk, not just the (possibly
    # --max-runs-sliced) subset we're rendering: the mid-loop recheck below
    # compares against the full unsliced _archived_run_inits() result, so
    # seeding this from `queue` alone made every run excluded by --max-runs
    # look "newly appeared" on the first recheck - turning a --max-runs 1
    # smoke test into a full backfill of every archived run.
    seen = set(all_archived)
    while queue:
        run_init = queue.pop(0)
        state["current_run"] = _iso_z(run_init)
        _write_progress(state)

        result = render_run(model_id, run_init)
        n_steps = len(result)
        n_with_data = sum(1 for fields in result.values() if any(fields.values()))
        log.info(
            "%s %s: rendered %d step(s), %d with real data",
            model_id, run_init.isoformat(), n_steps, n_with_data,
        )
        model_state["completed"].append({
            "run_init": _iso_z(run_init),
            "steps": n_steps,
            "steps_with_data": n_with_data,
            "finished_at": _iso_z(datetime.now(UTC)),
        })
        state["current_run"] = None
        _write_progress(state)

        new_arrivals = [r for r in _archived_run_inits(model_id) if r not in seen]
        if new_arrivals:
            new_arrivals.sort(reverse=True)  # newest of the new arrivals first
            log.info(
                "%s: %d new run(s) appeared mid-backfill, prioritizing: %s",
                model_id, len(new_arrivals), [r.isoformat() for r in new_arrivals],
            )
            queue = new_arrivals + queue
            seen.update(new_arrivals)
            model_state["planned_runs"] += len(new_arrivals)


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

    for model_id in models:
        _render_model(model_id, args.max_runs, state)

    state["current_model"] = None
    state["done"] = True
    _write_progress(state)
    log.info("backfill done")


if __name__ == "__main__":
    main()
