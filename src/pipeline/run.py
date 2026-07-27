"""Production pipeline entrypoint. DRY-RUN BY DEFAULT.

    python -m src.pipeline.run --sweep              # what would be deleted, right now
    python -m src.pipeline.run --sweep --verbose    # ...file by file, with reasons
    python -m src.pipeline.run --sizing             # peak-disk bound per model
    python -m src.pipeline.run                      # one full pass, plan only
    python -m src.pipeline.run --apply              # one full pass, really deletes
    python -m src.pipeline.run --loop --apply       # the production container's command

Nothing deletes without --apply. --apply additionally requires
config/production.yaml's reclaim.enabled (see src/pipeline/reclaim.py), so a
box can be locked read-only from config alone.

The desktop archiver is unaffected: its container runs
`python -m src.scheduler.run`, which never imports this package.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from datetime import UTC, datetime

from src.config import get_model, load_models
from src.pipeline import chunking, coverage, orchestrator, reclaim, verify
from src.pipeline.settings import load_settings

# How often the coverage matrix is rebuilt, independent of the pass loop.
COVERAGE_INTERVAL_S = 60.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline.run")

CHECK_INTERVAL_SECONDS = 300  # same cadence as the desktop scheduler's loop


def _gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB"


def print_pass(result: orchestrator.PassResult, verbose: bool = False) -> None:
    mode = "APPLY" if result.applied else "DRY-RUN (nothing deleted)"
    print(f"\n=== production pipeline pass [{mode}] ===")
    print(f"free disk: {_gb(result.free_bytes_before)} -> {_gb(result.free_bytes_after)}")
    print(f"reclaimable/reclaimed: {_gb(result.bytes_reclaimed)}   held: {_gb(result.bytes_held)}")
    for o in result.outcomes:
        bits = []
        if o.chunks:
            bits.append(f"{o.chunks} window(s)")
        if o.steps_rendered:
            bits.append(f"{o.steps_rendered} step(s) rendered")
        if o.extracted_rows is not None:
            bits.append(f"{o.extracted_rows} rows extracted")
        if o.files_reclaimed:
            verb = "deleted" if result.applied else "would delete"
            bits.append(f"{verb} {o.files_reclaimed} file(s) / {_gb(o.bytes_reclaimed)}")
        if o.bytes_held:
            bits.append(f"held {_gb(o.bytes_held)}")
        if o.skipped:
            bits.append(f"SKIPPED: {o.skipped}")
        if bits:
            print(f"  {o.model:<16} {o.run_init:%Y-%m-%dT%HZ}  " + ", ".join(bits))
        for e in o.errors:
            print(f"      error: {e}")
    if result.needs_attention:
        print("\n  NEEDS ATTENTION:")
        for m in result.needs_attention:
            print(f"    - {m}")
    if result.errors:
        print("\n  ERRORS:")
        for e in result.errors:
            print(f"    - {e}")
    if verbose:
        print("\n(per-file detail: rerun with --sweep --verbose)")


def print_sweep_detail(settings, models: list[str] | None, now: datetime) -> None:
    """Every raw file on disk, with the reclaim/hold decision and reason.
    Read-only; this is the report to read before ever passing --apply."""
    all_models = load_models()["models"]
    for model_id, model_config in all_models.items():
        if models and model_id not in models:
            continue
        if "cycles" not in model_config or "fetch" not in model_config:
            continue
        for run_init in orchestrator._archived_run_inits(model_id):
            plan = reclaim.plan_run(model_id, model_config, run_init, settings, now=now)
            if not plan.candidates:
                continue
            print(f"\n{model_id} {run_init:%Y-%m-%dT%HZ}")
            by_decision: dict[str, list] = {}
            for c in plan.candidates:
                by_decision.setdefault(c.decision, []).append(c)
            for decision, cands in sorted(by_decision.items()):
                total = sum(c.bytes for c in cands)
                print(f"  {decision:<38} {len(cands):>4} file(s)  {_gb(total)}")
                if decision != reclaim.RECLAIM:
                    reasons = {c.reason for c in cands if c.reason}
                    for r in sorted(reasons)[:3]:
                        print(f"      reason: {r}")
                else:
                    for c in cands[:5]:
                        print(f"      {c.path.name}  steps={c.steps}  {_gb(c.bytes)}")
                    if len(cands) > 5:
                        print(f"      ... and {len(cands) - 5} more")


def print_sizing(settings, now: datetime) -> None:
    """The peak raw footprint this design guarantees, per model, measured
    against whatever is already archived on this box."""
    from src.fetchers.base import cycle_run_inits

    print("\n=== peak in-flight raw footprint per model (measured) ===")
    print(f"{'model':<16} {'chunk':>7} {'B/step':>10} {'steps/window':>13} {'PEAK':>10}  mode")
    total_worst = 0
    for model_id, model_config in load_models()["models"].items():
        if "cycles" not in model_config or "fetch" not in model_config:
            continue
        inits = cycle_run_inits(model_config["cycles"], now, lookback_hours=24)
        if not inits:
            continue
        run_init = inits[-1]
        ch = settings.chunk_hours(model_id)
        per_step = chunking.bytes_per_step(model_id, settings.fallback_bytes_per_step)
        caps = chunking.chunk_caps(model_config, run_init, ch)
        widest, prev = 0, None
        for cap in caps:
            widest = max(widest, len(chunking.steps_in_chunk(model_config, run_init, cap, prev)))
            prev = cap
        renderable = verify.is_renderable(model_id)
        if not renderable:
            # Never reclaimed, so "peak in flight" is not the right question -
            # what it costs is its whole measured run, kept forever. Reported
            # honestly rather than as a bytes-per-step extrapolation.
            measured = chunking.measured_run_bytes(model_id)
            shown = f"{measured / 1024**3:>9.2f}G" if measured else "      n/a"
            print(f"{model_id:<16} {ch:>6}h {'-':>10} {'-':>13} {shown}  "
                  f"kept forever (no renderer), measured whole run")
            continue
        peak = chunking.peak_raw_bytes(
            model_id, model_config, run_init, ch, settings.fallback_bytes_per_step
        )
        mode = "windowed" if chunking.is_chunkable(model_config) else "whole run (not windowable)"
        print(f"{model_id:<16} {ch:>6}h {per_step / 1024**2:>9.0f}M {widest:>13} "
              f"{peak / 1024**3:>9.2f}G  {mode}")
        total_worst = max(total_worst, peak)
    print(f"\nworst single in-flight run (reclaimable models): "
          f"{total_worst / 1024**3:.2f} GB (disk floor {settings.min_free_gb} GB on top)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete reclaimable raw data (default: plan only)")
    parser.add_argument("--sweep", action="store_true",
                        help="reclaim-only pass: no fetching, no rendering")
    parser.add_argument("--sizing", action="store_true",
                        help="print the peak-disk bound per model and exit")
    parser.add_argument("--loop", action="store_true",
                        help="run continuously (the production container's mode)")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_SECONDS,
                        help=f"seconds between passes in --loop (default {CHECK_INTERVAL_SECONDS})")
    parser.add_argument("--model", action="append", dest="models",
                        help="restrict to this model (repeatable)")
    parser.add_argument("--verbose", action="store_true",
                        help="with --sweep: list every file and the reason for its decision")
    args = parser.parse_args()

    settings = load_settings()
    if args.models:
        for m in args.models:
            get_model(m)  # fail fast on a typo

    if args.sizing:
        print_sizing(settings, datetime.now(UTC))
        return

    if args.apply and not settings.reclaim_enabled:
        log.warning("--apply given but reclaim.enabled is false in config - "
                    "this pass will fetch/render but delete nothing")

    def one_pass() -> None:
        now = datetime.now(UTC)
        orchestrator.ping_healthcheck("/start")
        try:
            if args.sweep:
                result = orchestrator.sweep_all(settings, apply=args.apply, now=now,
                                                models=args.models)
                if args.verbose:
                    print_sweep_detail(settings, args.models, now)
            else:
                result = orchestrator.run_once(settings, apply=args.apply, now=now,
                                               models=args.models)
            print_pass(result, verbose=args.verbose)
            orchestrator.write_status(result, settings)
            orchestrator.append_history(result)
            coverage.write(now)
            orchestrator.ping_healthcheck("" if not result.errors else "/fail")
        except Exception:
            log.exception("pass failed")
            orchestrator.ping_healthcheck("/fail")

    if not args.loop:
        coverage.write()
        one_pass()
        return

    # Coverage is a disk scan (~3 s) and says nothing about the pass in flight,
    # so tying it to pass completion made the dashboard blank for exactly as
    # long as a first pass takes - which is when you most want to look at it.
    # It runs on its own clock instead. A thread is fine here: it only reads
    # directory listings and writes one small file, touching no eccodes or
    # matplotlib state.
    def _coverage_forever() -> None:
        while True:
            try:
                coverage.write()
            except Exception:
                log.exception("coverage refresh failed; will retry")
            time.sleep(COVERAGE_INTERVAL_S)

    threading.Thread(target=_coverage_forever, name="coverage", daemon=True).start()

    log.info("production pipeline starting (mode=%s, interval=%ds)",
             "apply" if args.apply else "dry-run", args.interval)
    while True:
        one_pass()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
