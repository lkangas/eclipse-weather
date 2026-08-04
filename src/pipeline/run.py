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
import os
import threading
import time
from datetime import UTC, datetime

from src.config import DATA_ROOT, get_model, load_models
from src.pipeline import chunking, coverage, frame_reclaim, orchestrator, reclaim, verify
from src.pipeline.settings import load_settings

# How often the coverage matrix is rebuilt, independent of the pass loop.
COVERAGE_INTERVAL_S = 60.0

# Gap between passes while there is still work to do. Small but not zero: it
# keeps a pathological loop (a run that always reports work but never finishes)
# from becoming a hot spin against upstream.
BUSY_INTERVAL_S = float(os.environ.get("BUSY_INTERVAL_S", "15"))

# How often the tool manifests are rebuilt, independent of the pass loop.
# 60 s, not the original 300: regeneration used to cost 86 s (models.yaml was
# re-parsed on every get_model call) and now costs 1.7 s, so the old interval
# was sized against a cost that no longer exists. As the eclipse approaches the
# newest run is the one worth seeing, and five minutes of staleness on a 1.7 s
# job is not a trade worth making.
MANIFEST_INTERVAL_S = float(os.environ.get("MANIFEST_INTERVAL_S", "60"))

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


def print_frame_sweep(frames_dir, keep_runs: int, models: list[str] | None, *, apply: bool) -> None:
    """Rendered-frame prune report: newest `keep_runs` run-inits/model kept,
    everything older listed for deletion. Plan only unless apply=True."""
    plans = frame_reclaim.plan_all(frames_dir, keep_runs, models=models)
    total_freed = 0
    total_files = 0
    for p in plans:
        if not p.to_prune and not p.pruned_run_inits:
            continue
        gb = p.bytes_to_prune / 1024**3
        print(f"{p.model:<16} keep {len(p.kept_run_inits)} run(s), "
              f"prune {len(p.pruned_run_inits)} run(s) / {len(p.to_prune)} file(s) / {gb:.2f} GB")
        if p.unparsed:
            print(f"      ({len(p.unparsed)} file(s) with an unrecognised name - left untouched)")
        total_files += len(p.to_prune)
        total_freed += p.bytes_to_prune
        if apply:
            frame_reclaim.apply_plan(p)
    verb = "deleted" if apply else "would delete"
    print(f"\n{verb} {total_files} file(s) / {total_freed / 1024**3:.2f} GB total "
          f"(keep-runs={keep_runs})")
    if not apply:
        print("dry run - rerun with --apply to actually delete")


def print_sizing(settings, now: datetime) -> None:
    """The peak raw footprint this design guarantees, per model, measured
    against whatever is already archived on this box."""
    from src.fetchers.base import cycle_run_inits

    print("\n=== peak in-flight raw footprint per model (measured) ===")
    print("PEAK is the WHOLE RUN: reclaim runs when the pre-fetch headroom check")
    print("says the next window would not fit, not after every window, so fetched")
    print("windows accumulate until then. WINDOW is what chunking does bound - the")
    print("most that can be committed between two chances to stop.")
    print(f"{'model':<16} {'chunk':>7} {'B/step':>10} {'WINDOW':>9} {'PEAK':>10}  mode")
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
        window = chunking.window_increment_bytes(
            model_id, model_config, run_init, ch, settings.fallback_bytes_per_step
        )
        mode = (f"windowed, {widest} step(s)/window" if chunking.is_chunkable(model_config)
                else "whole run (not windowable)")
        win_s = f"{window / 1024**3:>8.2f}G" if window else "       -"
        print(f"{model_id:<16} {ch:>6}h {per_step / 1024**2:>9.0f}M {win_s} "
              f"{peak / 1024**3:>9.2f}G  {mode}")
        total_worst = max(total_worst, peak)
    print(f"\nworst single in-flight run (reclaimable models): "
          f"{total_worst / 1024**3:.2f} GB (disk floor {settings.min_free_gb} GB on top)")
    print("The floor is the real guarantee: the headroom check reclaims before "
          "fetching a\nwindow that would breach it. This figure says how much "
          "free space a single run\ncan consume before that happens.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually delete reclaimable raw data (default: plan only)")
    parser.add_argument("--sweep", action="store_true",
                        help="reclaim-only pass: no fetching, no rendering")
    parser.add_argument("--sizing", action="store_true",
                        help="print the peak-disk bound per model and exit")
    parser.add_argument("--sweep-frames", action="store_true",
                        help="rendered-frame prune pass: plan only unless --apply too, then exit "
                             "(see src/pipeline/frame_reclaim.py)")
    parser.add_argument("--keep-runs", type=int, default=None,
                        help="with --sweep-frames: newest N run-inits/model to keep "
                             "(default: config/production.yaml's frames.max_runs_per_model)")
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

    if args.sweep_frames:
        keep = args.keep_runs if args.keep_runs is not None else settings.max_runs_per_model
        if keep is None:
            print("no --keep-runs given and frames.max_runs_per_model is unset (null) - "
                  "nothing to do, refusing to guess a cutoff")
            return
        print_frame_sweep(DATA_ROOT / "viz" / "frames", keep, args.models, apply=args.apply)
        return

    if args.apply and not settings.reclaim_enabled:
        log.warning("--apply given but reclaim.enabled is false in config - "
                    "this pass will fetch/render but delete nothing")

    def one_pass():
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
            did_work = any(o.chunks or o.steps_rendered for o in result.outcomes)
            orchestrator.write_status(result, settings)
            orchestrator.append_history(result)
            coverage.write(now)
            orchestrator.ping_healthcheck("" if not result.errors else "/fail")
            return did_work
        except Exception:
            log.exception("pass failed")
            orchestrator.ping_healthcheck("/fail")
        return False

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

    # Manifests are THE product - the tools read them, not the frame tree - yet
    # they were regenerated only at the end of a pass. A pass on this box has
    # never once returned, so the manifests sat 4h20m behind frames that were
    # already on disk: the work was done and invisible. They are a pure disk
    # scan (~0.5-7 s, reads no raw), so they belong on a clock of their own for
    # exactly the same reason coverage does.
    def _manifests_forever() -> None:
        from src.pipeline import render as pipeline_render
        while True:
            time.sleep(MANIFEST_INTERVAL_S)
            try:
                pipeline_render.regenerate_manifests()
            except Exception:
                log.exception("manifest refresh failed; will retry")

    threading.Thread(target=_manifests_forever, name="manifests", daemon=True).start()

    log.info("production pipeline starting (mode=%s, interval=%ds)",
             "apply" if args.apply else "dry-run", args.interval)
    while True:
        # Only wait out the full interval when there was nothing to do. The
        # interval exists to avoid hammering upstream when idle - it was never
        # meant to throttle catch-up. With max_chunks_per_pass capping a run at
        # 4 windows, a 17-chunk run needs 4-6 passes; at a flat 300 s that is
        # ~25 minutes per run, almost all of it spent asleep while 10 runs sat
        # part-fetched. A pass now takes ~54 s, so when a pass did real work
        # there is no reason to pause before the next one.
        if one_pass():
            time.sleep(BUSY_INTERVAL_S)
        else:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
