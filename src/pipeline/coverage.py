"""Did every model actually get every run it should have?

The status file says what a pass DID; it cannot say what never happened. A run
that was never fetched produces no outcome, no error and no log line - it is
simply absent, which is indistinguishable from "not due yet" unless something
computes the expectation independently. icon_eu and icon_global both lost
2026-07-25T12Z that way: the fetch errored, the run stayed empty, and it only
surfaced 36 hours later as a hole in an unrelated progress strip. By then DWD's
~24 h retention had closed and it was unrecoverable.

Coverage is judged on RENDERED FRAMES, not on raw. Production deletes raw as
soon as it has been rendered and verified, so raw presence means "recent", not
"archived" - judging by it would report every successfully-processed run as
missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

LOOKBACK_H = 48

# How long past its publication deadline a run may sit un-fetched before it is
# a fault rather than a wait. Generous: publication_lag_h is an upper bound
# already, and a pass can legitimately take a while to reach a given model.
OVERDUE_AFTER_H = 3.0

# Proportion of a run's declared steps that must have frames before it counts
# as rendered. Not 1.0: a step that publishes nothing produces no frame by
# design, so a healthy gfs run lands at 208/209.
COMPLETE_FRACTION = 0.9


def _frame_index(model_id: str) -> dict[str, dict[str, int]]:
    """{run_stamp: {field: n_frames}} from ONE listing per field directory.

    The obvious implementation - ask "does this run have frames?" per run, per
    field - re-lists a directory holding thousands of files once per question,
    which made building the matrix slow enough that it was only worth doing at
    the end of a pass. That is precisely backwards: this is the panel you want
    while a long pass is still running.
    """
    from src.viz.frame_renderer import OUTPUT_DIR, supported_fields
    idx: dict[str, dict[str, int]] = {}
    for field in supported_fields(model_id):
        d = OUTPUT_DIR / model_id / field
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix != ".png":
                continue
            stamp = p.name.split("_", 1)[0]
            idx.setdefault(stamp, {})
            idx[stamp][field] = idx[stamp].get(field, 0) + 1
    return idx


def _latest_only(cfg: dict) -> bool:
    """Does this source serve only whatever run is current? (aemet_harmonie)"""
    return bool((cfg.get("source", {}).get("open_endpoint") or {})
                .get("serves_latest_run_only"))


def _publish_complete_h(cfg: dict) -> float:
    """Hours after init by which this model has published its whole range.

    Read from models.yaml rather than guessed: publication_lag_h's upper bound
    covers the ordinary case, and a model that releases part of its range much
    later declares that separately - gefs_extended's extended 385-840h block
    lands 25-27h after init, long after the lag that covers its first 384h.
    """
    lag = cfg.get("publication_lag_h") or [0, 0]
    base = float(lag[1] if len(lag) > 1 else lag[0])
    return float(cfg.get("full_range_published_by_h", base))


def _raw_state(model_id: str, run_init: datetime) -> tuple[int, bool]:
    """(count of real raw files, tombstoned?) for this run.

    Dotfiles are excluded: a failed fetch leaves .extracted / .last_fetch_attempt
    behind, and counting those as raw would make an empty run look fetched.
    The tombstone (.reclaimed.json) is what separates "raw deleted after a
    verified render" from "raw never arrived" - without it those two look
    identical from disk, and they are opposite ends of the lifecycle.
    """
    from src.config import DATA_RAW
    from src.pipeline.journal import RECLAIMED_FILE
    d = DATA_RAW / model_id / f"{run_init:%Y%m%d%H}"
    if not d.is_dir():
        return 0, False
    n = 0
    tomb = False
    for p in d.iterdir():
        if p.name == RECLAIMED_FILE:
            tomb = True
        elif p.is_file() and not p.name.startswith("."):
            n += 1
    return n, tomb


def build(now: datetime | None = None, lookback_h: int = LOOKBACK_H) -> dict:
    """Per-run LIFECYCLE state for every cycle slot in the window.

    A run moves: future -> available -> fetched -> ready -> done. The two
    in-flight states (fetching, rendering) are not on disk at all - they come
    from pipeline_activity.json and are overlaid by the page, because only the
    running pass knows them.

      future    - upstream has not published it yet (now < due_at)
      available - due, and nothing has arrived. Waiting to be fetched.
      overdue   - available for far longer than it should be. A real fault:
                  upstream retention is short, so this is where runs are lost.
      missed    - past, and unfetchable: the source serves only its current
                  run, so this cycle can never be retrieved. Not actionable.
      fetched   - raw on disk, not yet rendered (or only partly)
      partial-upstream
                - every step the model has PUBLISHED is rendered, but the run
                  is not complete because the rest does not exist yet. Nothing
                  to do; it completes itself when upstream catches up.
      ready     - every supported field rendered, raw still on disk to reclaim
      done      - fetched, rendered and reclaimed by THIS box. Terminal.
      seeded    - frames present but raw never was: migrated in from the
                  desktop archive. Also terminal, but this box never extracted
                  its points, which matters for being self-sufficient.
      no-render - model has no renderer, so frames can never be the evidence;
                  its raw is kept indefinitely and it never reaches done.
    """
    from src.config import load_models
    from src.fetchers.base import (
        FETCH_TOPUP_WINDOW_H,
        cycle_run_inits,
        due_time,
        full_range_steps,
    )
    from src.viz.frame_renderer import _MODEL_READERS, supported_fields

    now = now or datetime.now(UTC)
    out: dict[str, list[dict]] = {}
    next_up: dict[str, dict] = {}
    for model_id, cfg in load_models()["models"].items():
        if "cycles" not in cfg or "fetch" not in cfg:
            continue
        renderable = model_id in _MODEL_READERS
        n_fields = len(supported_fields(model_id)) if renderable else 0
        index = _frame_index(model_id) if renderable else {}
        rows = []
        for run_init in cycle_run_inits(cfg["cycles"], now, lookback_hours=lookback_h):
            due = due_time(cfg.get("publication_lag_h", [0, 0]), run_init)
            per_field = index.get(f"{run_init:%Y%m%d%H}", {})
            frames = sum(per_field.values())
            raw_n, tombstoned = _raw_state(model_id, run_init)
            # Completeness has to be about STEPS, not fields. "every field has
            # at least one frame" called a run complete after its first chunk
            # of 17, so a run 6% processed looked finished. Steps that
            # genuinely publish nothing produce no frame by design (gfs f000),
            # so this is a proportion rather than equality.
            expected = len(full_range_steps(cfg, run_init)) or 1
            complete = (
                renderable and n_fields and len(per_field) == n_fields
                and min(per_field.values()) >= expected * COMPLETE_FRACTION
            )
            # Incomplete for a reason that is not ours: the model has not
            # published the rest yet. gefs_extended is the case that keeps
            # surfacing - NOAA releases its 385-840h range 25-27h after init,
            # so a 20h-old run legitimately sits at 104/121 with every
            # PUBLISHED step already rendered, and reporting that as "fetched"
            # reads as work outstanding when there is nothing to fetch.
            waiting_upstream = (
                renderable and not complete and per_field
                and (now - run_init).total_seconds() / 3600 < _publish_complete_h(cfg)
            )

            if now < due:
                state = "future"
            elif not renderable:
                # cma_grapes_global, gem_global, jma_gsm, ukmo_global: Open-Meteo
                # point-API models with no grid and no reader. They can never
                # produce a frame, so judging them on frames left them stuck at
                # available -> overdue permanently, reporting 30+ hours "late"
                # for work that is never going to happen. They contribute point
                # rows to points.parquet, not maps; the lifecycle simply does
                # not apply, and saying so is more honest than a red cell.
                state = "no-render"
            elif complete:
                # Tombstone first, because it is MONOTONIC and raw presence is
                # not. A finished run stays inside the 48 h top-up window, so
                # every pass re-fetches it: raw reappears, nothing new renders,
                # raw is reclaimed again. Keying on raw made such a run
                # oscillate done -> ready -> done once per pass forever, and
                # emit a timeline event each way. Once this box has reclaimed a
                # complete run it is done; transient top-up raw is work
                # happening TO it, which the busy indicator already shows.
                if tombstoned:
                    state = "done"         # fetched, rendered and reclaimed here
                elif raw_n:
                    state = "ready"        # rendered here, awaiting first reclaim
                else:
                    state = "seeded"       # frames arrived from elsewhere
            elif waiting_upstream:
                state = "partial-upstream"
            elif raw_n:
                state = "fetched"
            elif tombstoned:
                # Raw gone and steps incomplete. Only a loss once the run can
                # no longer gain steps: gefs_extended publishes its 385-840h
                # range 25-27 h AFTER init, so a 6 h old run legitimately sits
                # at 104/121 steps and a flat percentage called that "lost".
                # The top-up window is the existing notion of "can this still
                # grow", so use it rather than inventing a second rule.
                sealed = (now - run_init).total_seconds() / 3600 >= FETCH_TOPUP_WINDOW_H
                state = "gone" if sealed else "fetched"
            else:
                overdue_h = (now - due).total_seconds() / 3600
                if overdue_h <= OVERDUE_AFTER_H:
                    state = "available"
                elif _latest_only(cfg):
                    # A source that serves only its current run cannot be asked
                    # for a past cycle, so "overdue" is wrong: it implies
                    # someone could still fetch it. Once the window moved on,
                    # that cycle is simply gone. Saying so stops six permanently
                    # unfixable cells sitting red for up to 48 h and training
                    # the eye to ignore the colour that means act now.
                    state = "missed"
                else:
                    state = "overdue"

            rows.append({
                "run_init": run_init.isoformat().replace("+00:00", "Z"),
                "hour": f"{run_init:%H}Z",
                "day": f"{run_init:%m-%d}",
                "state": state,
                "frames": frames,
                "raw_files": raw_n,
                "reclaimed": tombstoned,
                "due_at": due.isoformat().replace("+00:00", "Z"),
                "age_h": round((now - run_init).total_seconds() / 3600, 1),
            })
        # Chronological, oldest first: the row then reads left-to-right as the
        # lifecycle itself runs, with future slots at the right edge.
        rows.sort(key=lambda r: r["run_init"])

        # "What happens next" is a different question from "what state is
        # everything in", and the matrix could not answer it: every cell is a
        # run that already exists in the schedule, so the next thing the
        # pipeline will actually do was nowhere on the page.
        nxt = next((r for r in rows if r["state"] in ("available", "overdue")), None)
        upcoming = next((r for r in rows if r["state"] == "future"), None)
        out[model_id] = rows
        next_up[model_id] = {
            "waiting": {"run_init": nxt["run_init"], "hour": nxt["hour"],
                        "state": nxt["state"], "due_at": nxt["due_at"]} if nxt else None,
            "future": {"run_init": upcoming["run_init"], "hour": upcoming["hour"],
                       "due_at": upcoming["due_at"]} if upcoming else None,
        }
    return {
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "lookback_h": lookback_h,
        "models": out,
        "next_up": next_up,
    }


EVENTS_FILENAME = "pipeline_events.jsonl"
EVENTS_MAX_ROWS = 4000

# Transitions not worth a line: they are noise, not events.
_BORING = {("future", "available"), ("available", "future")}


def _emit_events(prev: dict, cur: dict, path) -> None:
    """Append one line per run whose lifecycle state changed.

    Derived by diffing successive coverage snapshots rather than emitted from
    inside the pipeline. One place produces every event, including the ones no
    single code path owns - a run becoming available is upstream's doing and
    the pipeline never executes anything at that moment, so there is nowhere
    in the pass to emit it from.
    """

    prev_states = {
        (m, r["run_init"]): r["state"]
        for m, rows in (prev.get("models") or {}).items() for r in rows
    }
    from src.pipeline.orchestrator import append_event
    for model, rows in cur["models"].items():
        for r in rows:
            was = prev_states.get((model, r["run_init"]))
            if was == r["state"] or (was, r["state"]) in _BORING:
                continue
            if was is None and r["state"] in ("future", "available"):
                continue        # first sighting of a slot is not an event
            append_event({
                "kind": "state", "at": cur["updated_at"], "model": model,
                "run_init": r["run_init"], "hour": r["hour"], "day": r["day"],
                "from": was, "to": r["state"],
                "frames": r["frames"], "raw_files": r["raw_files"],
            })


def write(now: datetime | None = None) -> None:
    import json

    from src.viz import frame_renderer
    try:
        out = frame_renderer.OUTPUT_DIR
        path = out / "pipeline_coverage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
        cur = build(now)
        _emit_events(prev, cur, out / EVENTS_FILENAME)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
