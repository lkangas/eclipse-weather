"""The production pass: for every due run, fetch a window -> render it ->
extract -> verify -> reclaim that window's raw -> next window.

Ordering is the whole point, so it is spelled out once here:

    for each model, for each candidate run_init (same due/top-up rules the
    desktop scheduler uses - src/fetchers/base.py):
        for each forecast-hour window of that run (src/pipeline/chunking.py):
            0. refuse to start the window if free disk would fall below the
               configured floor (reclaim first, then skip the model - never
               fill the disk)
            1. fetch the window          (unchanged fetcher, narrowed config)
            2. render the window's steps (unchanged frame_renderer)
            3. extract the run to points.parquet as soon as every eclipse
               archive step it will ever supply is on disk
            4. verify frames on disk, then delete only what is verified
    then, once per pass: regenerate the Tool 1/2/3 manifests

Step 4 never runs speculatively and never assumes step 1-3 succeeded: it
re-reads the rendered tree from disk (src/pipeline/verify.py).

MERGE SEAMS - a separate agent is concurrently adding to
src/scheduler/run.py: (a) corrupt-file detection with re-fetch, (b)
re-rendering runs that gained steps, (c) automatic rendering after fetch,
(d) automatic manifest regeneration after rendering. This module assumes
(c) and (d) will exist there and deliberately does NOT touch run.py, so the
desktop keeps its own behaviour and there is nothing to merge in that file.
Where the two designs meet:

  * (a) corrupt-file detection must consult
    journal.reclaimed_filenames()/reclaimed_steps() before calling a missing
    file corrupt: in production a missing file usually means "rendered and
    deliberately discarded", not "bad download". src.fetchers.base's
    raw_file_present() already encodes that test for the fetch side.
  * (b) re-rendering a run that gained steps must likewise skip reclaimed
    steps - their frames already exist and their raw is intentionally gone.
  * (c)/(d) if they land as reusable helpers, src/pipeline/render.py's two
    functions should collapse into calls to them.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from src import config
from src.config import load_models
from src.pipeline import chunking, failures, reclaim, render, verify
from src.pipeline.settings import Settings

log = logging.getLogger("pipeline")


@dataclass
class RunOutcome:
    model: str
    run_init: datetime
    chunks: int = 0
    steps_rendered: int = 0
    extracted_rows: int | None = None
    bytes_reclaimed: int = 0
    bytes_held: int = 0
    files_reclaimed: int = 0
    skipped: str | None = None
    errors: list[str] = field(default_factory=list)
    needs_attention: list[str] = field(default_factory=list)


@dataclass
class PassResult:
    started_at: datetime
    finished_at: datetime | None = None
    applied: bool = False
    free_bytes_before: int = 0
    free_bytes_after: int = 0
    outcomes: list[RunOutcome] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(o.bytes_reclaimed for o in self.outcomes)

    @property
    def bytes_held(self) -> int:
        return sum(o.bytes_held for o in self.outcomes)

    @property
    def needs_attention(self) -> list[str]:
        return [f"{o.model} {o.run_init:%Y%m%d%H}: {m}" for o in self.outcomes
                for m in o.needs_attention]


# --------------------------------------------------------------------------
# disk
# --------------------------------------------------------------------------


def free_bytes() -> int:
    try:
        return shutil.disk_usage(config.DATA_ROOT).free
    except OSError:
        return 0


def _headroom_ok(needed: int, settings: Settings) -> bool:
    return free_bytes() - needed >= settings.min_free_bytes


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------


def _reclaim_run(
    model_id: str, model_config: dict, run_init: datetime, settings: Settings,
    outcome: RunOutcome, *, apply: bool, now: datetime,
) -> None:
    plan = reclaim.plan_run(model_id, model_config, run_init, settings, now=now)
    outcome.bytes_held += plan.bytes_held
    outcome.needs_attention.extend(plan.needs_attention)
    if not plan.to_reclaim:
        return
    if apply:
        outcome.bytes_reclaimed += reclaim.apply_plan(plan, settings, now=now)
        outcome.files_reclaimed += len(plan.to_reclaim)
    else:
        outcome.bytes_reclaimed += plan.bytes_to_reclaim
        outcome.files_reclaimed += len(plan.to_reclaim)
        for c in plan.to_reclaim:
            log.info("[dry-run] would delete %s (%.1f MB, steps %s)",
                     c.path, c.bytes / 1024**2, c.steps)


def _frames_complete(model_id: str, run_init: datetime,
                     now: datetime | None = None) -> bool:
    """Does this run already have every frame it can produce?

    Cheap: directory listings only, no raw touched.

    The rule lives in src/pipeline/completeness.py and is shared with the
    dashboard - this used to hold its own hardcoded 0.9 while coverage.py held
    COMPLETE_FRACTION, two copies of a judgement that has to agree. It is also
    no longer a fraction: 90% of gefs_extended's declared steps is more than
    its whole extended range, so a run missing +432..+480h read as done and was
    skipped with fetch=False while the data sat on AWS.
    """
    from src.config import get_model
    from src.fetchers.base import full_range_steps
    from src.pipeline import completeness
    from src.viz.frame_renderer import OUTPUT_DIR, supported_fields

    fields = supported_fields(model_id)
    if not fields:
        return False
    declared = full_range_steps(get_model(model_id), run_init)
    stamp = f"{run_init:%Y%m%d%H}_"
    frames: dict[str, set[int]] = {}
    for fld in fields:
        d = OUTPUT_DIR / model_id / fld
        if not d.is_dir():
            return False
        steps = set()
        for p in d.iterdir():
            if not p.name.startswith(stamp) or p.suffix != ".png":
                continue
            try:
                steps.add(int(p.stem.split("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        frames[fld] = steps
    return completeness.is_complete(
        model_id, run_init, declared, frames, fields, now or datetime.now(UTC))


def _maybe_extract(
    model_id: str, model_config: dict, run_init: datetime, outcome: RunOutcome
) -> None:
    """Extract to points.parquet once - and only once - every eclipse archive
    step this run will ever supply is on disk.

    The desktop scheduler extracts as soon as any files exist, which is fine
    when raw is kept forever: a later top-up simply adds files nobody
    re-reads. Production cannot do that, because the raw would be deleted
    before the late-arriving eclipse steps ever reached points.parquet -
    exactly the GEFS case (extended range lands ~25-27 h after init, and for
    an early run those late steps ARE the eclipse hours). So the gate here is
    "extraction-ready", not "anything fetched", and reclaim.py independently
    refuses to delete any eclipse-bearing file until .extracted exists.
    """
    from src.extract import registry as extract_registry
    from src.extract.base import already_extracted, append_points, mark_extracted

    if already_extracted(model_id, run_init):
        return
    v = verify.verify_run(model_id, model_config, run_init)
    if not v.extraction_ready:
        return
    try:
        extractor = extract_registry.get_extractor(model_config["fetch"])
        rows = extractor(model_id, model_config, run_init)
        append_points(rows)
        mark_extracted(model_id, run_init)
        outcome.extracted_rows = len(rows)
        log.info("extracted %s %s: %d rows", model_id, run_init.isoformat(), len(rows))
    except Exception as e:
        outcome.errors.append(f"extract: {e}")
        log.exception("extract failed for %s %s", model_id, run_init.isoformat())


def process_run(
    model_id: str,
    model_config: dict,
    run_init: datetime,
    settings: Settings,
    *,
    apply: bool,
    now: datetime,
    fetch: bool = True,
) -> RunOutcome:
    """Fetch (windowed), render, extract and reclaim one run."""
    from src.fetchers import registry as fetch_registry
    from src.fetchers.base import record_fetch_attempt

    outcome = RunOutcome(model=model_id, run_init=run_init)

    if not fetch:
        _maybe_extract(model_id, model_config, run_init, outcome)
        _reclaim_run(model_id, model_config, run_init, settings, outcome,
                     apply=apply, now=now)
        return outcome

    chunk_hours = settings.chunk_hours(model_id)
    caps = chunking.chunk_caps(model_config, run_init, chunk_hours)
    if not caps:
        outcome.skipped = "run publishes no step at or before data_horizon"
        return outcome
    if settings.max_chunks_per_pass:
        caps = caps[: settings.max_chunks_per_pass]

    per_step = chunking.bytes_per_step(model_id, settings.fallback_bytes_per_step)

    # NO dead-step filtering here. It was added on the belief that narrowing
    # `wanted` would keep those steps out of the fetch; it does not. `wanted`
    # only ever reaches render_steps() - the fetcher is handed a cycle CAP and
    # recomputes its own step list from full_range_steps(), so it re-attempts
    # every "skipped" step regardless. The filter therefore bought nothing and
    # cost something real: a step still inside the publication stagger gets
    # flagged dead, and its flag clears 24 h after the LAST failure (~init+50h)
    # while the top-up window shuts at init+48h. gefs_extended 2026-07-27T00Z's
    # eclipse steps (+396/402/408) were on course to be fetched and then never
    # rendered. The ledger below still records failures for the dashboard;
    # only the filtering is gone.

    previous_cap: int | None = None
    for cap in caps:
        wanted = chunking.steps_in_chunk(model_config, run_init, cap, previous_cap)
        previous_cap = cap
        if not wanted:
            continue

        needed = per_step * len(wanted)
        if not _headroom_ok(needed, settings):
            # Try to free space from everything already rendered, then re-test.
            log.warning(
                "low disk before %s %s window <=+%dh (need %.1f GB, free %.1f GB) - "
                "reclaiming first", model_id, run_init.isoformat(), cap,
                needed / 1024**3, free_bytes() / 1024**3,
            )
            sweep_all(settings, apply=apply, now=now)
            if not _headroom_ok(needed, settings):
                outcome.skipped = (
                    f"insufficient disk: need {needed / 1024**3:.1f} GB + "
                    f"{settings.min_free_gb} GB floor, free {free_bytes() / 1024**3:.1f} GB"
                )
                outcome.needs_attention.append(outcome.skipped)
                log.error("%s %s: %s", model_id, run_init.isoformat(), outcome.skipped)
                break

        publish_activity("fetching", model=model_id, run_init=run_init,
                         window_cap_h=cap, chunk=caps.index(cap) + 1, chunks=len(caps))
        narrowed = chunking.narrow_config(model_config, run_init, cap)
        try:
            fetcher = fetch_registry.get_fetcher(model_config["fetch"])
            result = fetcher(model_id, narrowed, run_init)
            # Partial failures carry status "ok" but a populated .error, so
            # record unconditionally rather than only on status == "error".
            # NOT the pass-level `now`: the ledger wants the time this window
            # actually failed, and a pass reaches this line for hours after it
            # started (see failures.record's own note).
            failures.record(model_id, run_init, result.error)
            if result.status == "error":
                outcome.errors.append(f"fetch <=+{cap}h: {result.error}")
        except Exception as e:
            outcome.errors.append(f"fetch <=+{cap}h: {e}")
            log.exception("fetch failed for %s %s window <=+%dh",
                          model_id, run_init.isoformat(), cap)
            break
        outcome.chunks += 1

        publish_activity("rendering", model=model_id, run_init=run_init,
                         window_cap_h=cap, chunk=caps.index(cap) + 1, chunks=len(caps))
        try:
            rendered = render.render_steps(model_id, run_init, wanted)
            outcome.steps_rendered += sum(1 for f in rendered.values() if any(f.values()))
        except Exception as e:
            outcome.errors.append(f"render <=+{cap}h: {e}")
            log.exception("render failed for %s %s window <=+%dh",
                          model_id, run_init.isoformat(), cap)
            # Do NOT reclaim after a failed render - fall through to the next
            # window, leaving this one's raw on disk for the retry.
            continue

        publish_activity("extracting", model=model_id, run_init=run_init)
        _maybe_extract(model_id, model_config, run_init, outcome)
        publish_activity("reclaiming", model=model_id, run_init=run_init)
        _reclaim_run(model_id, model_config, run_init, settings, outcome,
                     apply=apply, now=now)

    # Stamped only after the whole run has been walked, not before it starts.
    # should_attempt_fetch() then blocks this run for FETCH_RETRY_INTERVAL_H,
    # which is right for "we asked for everything and upstream had no more" but
    # wrong for "we were interrupted part way". Stamping up front meant any
    # interruption - a container restart, a crash, a break out of the chunk
    # loop on low disk - cost the run a full hour before it could be resumed,
    # while it sat visibly incomplete and the pipeline reported nothing to do.
    # Observed: gfs 2026-07-27T12Z stopped at 48 of 209 steps with every step
    # published upstream, then was skipped for an hour in favour of an older
    # run. A run that breaks out early is now simply retried on the next pass.
    record_fetch_attempt(model_id, run_init, now)
    return outcome


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------


def _archived_run_inits(model_id: str) -> list[datetime]:
    d = config.DATA_RAW / model_id
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_dir():
            continue
        try:
            out.append(datetime.strptime(p.name, "%Y%m%d%H").replace(tzinfo=UTC))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def sweep_all(
    settings: Settings, *, apply: bool, now: datetime | None = None,
    models: list[str] | None = None,
) -> PassResult:
    """Reclaim pass only - no fetching, no rendering. Every archived run of
    every model is verified against the rendered tree and anything provably
    redundant is planned (or, with apply, deleted).

    This is the mode to run first on a new production box, and the one the
    disk guard falls back to mid-fetch.
    """
    now = now or datetime.now(UTC)
    result = PassResult(started_at=now, applied=apply, free_bytes_before=free_bytes())
    all_models = load_models()["models"]
    for model_id, model_config in all_models.items():
        if models and model_id not in models:
            continue
        if "cycles" not in model_config or "fetch" not in model_config:
            continue
        for run_init in _archived_run_inits(model_id):
            outcome = RunOutcome(model=model_id, run_init=run_init)
            _reclaim_run(model_id, model_config, run_init, settings, outcome,
                         apply=apply, now=now)
            if outcome.files_reclaimed or outcome.bytes_held or outcome.needs_attention:
                result.outcomes.append(outcome)
    result.finished_at = datetime.now(UTC)
    result.free_bytes_after = free_bytes()
    return result


def run_once(
    settings: Settings, *, apply: bool, now: datetime | None = None,
    models: list[str] | None = None,
) -> PassResult:
    """One full production pass: due runs get fetched/rendered/extracted/
    reclaimed; every other archived run gets a reclaim-only visit."""
    from src.fetchers.base import already_fetched, cycle_run_inits, due_time, should_attempt_fetch

    now = now or datetime.now(UTC)
    result = PassResult(started_at=now, applied=apply, free_bytes_before=free_bytes())
    all_models = load_models()["models"]
    rendered_anything = False
    _fetchable = [m for m, c in all_models.items() if "cycles" in c and "fetch" in c]
    _pass_started = now

    # INIT-MAJOR, not model-major. The pass used to walk models.yaml in order
    # and drain each model's due runs before moving on, so gefs_extended
    # (entry 1) got even its OLDEST due run before gfs, ecmwf or icon were
    # touched at all - and a freshly published 12Z run sat waiting behind a
    # stale 00Z one from a different model. Sorting the whole candidate set by
    # init time instead means every model's newest run is fetched before any
    # model's second-newest, which is what "newest first" has to mean when the
    # tools show all models side by side. Model order now only breaks ties
    # between runs sharing an init hour.
    candidates: list[tuple[datetime, str, dict]] = []
    for model_id, model_config in all_models.items():
        if models and model_id not in models:
            continue
        if model_id in settings.exclude_models:
            continue  # see config/production.yaml exclude_models
        if "cycles" not in model_config or "fetch" not in model_config:
            continue  # aggregator/reference entries carry no fetch path
        for run_init in cycle_run_inits(model_config["cycles"], now):
            if now < due_time(model_config.get("publication_lag_h", [0, 0]), run_init):
                continue
            if not should_attempt_fetch(model_id, run_init, now):
                continue
            candidates.append((run_init, model_id, model_config))

    # Newest init first; models.yaml order preserved within an init by the
    # stable sort, so a shared cycle hour still goes in the documented order.
    candidates.sort(key=lambda c: c[0], reverse=True)

    due_by_model: dict[str, set] = {}
    for idx, (run_init, model_id, model_config) in enumerate(candidates, 1):
        due_by_model.setdefault(model_id, set()).add(run_init)
        publish_activity("model", model=model_id,
                         model_index=idx, models_total=len(candidates),
                         pass_started_at=_pass_started,
                         runs_done=len(result.outcomes),
                         gb_reclaimed_so_far=round(result.bytes_reclaimed / 1024**3, 3),
                         errors_so_far=len(result.errors) + sum(
                             len(o.errors) for o in result.outcomes))

        # Frames already complete -> nothing to fetch. This is the whole
        # seeded-archive case: 53 runs whose frames were migrated from the
        # desktop had no raw here, so already_fetched() said "never fetched"
        # and the pass re-downloaded entire runs - including ~16 GB aifs_ens
        # runs - to produce frames that already existed. The only thing that
        # re-fetch could add is point extraction, and per the agreed split that
        # is the DESKTOP's job: anything needing raw (point series,
        # line-of-sight) is developed there, where raw is kept, and backfilled
        # to the VPS. Production renders maps.
        if _frames_complete(model_id, run_init, now):
            outcome = process_run(model_id, model_config, run_init, settings,
                                  apply=apply, now=now, fetch=False)
            result.outcomes.append(outcome)
            continue
        try:
            outcome = process_run(model_id, model_config, run_init, settings,
                                  apply=apply, now=now)
        except Exception as e:
            result.errors.append(f"{model_id} {run_init.isoformat()}: {e}")
            log.exception("process_run failed for %s %s", model_id, run_init.isoformat())
            continue
        rendered_anything = rendered_anything or bool(outcome.steps_rendered)
        result.outcomes.append(outcome)

    for model_id, model_config in all_models.items():
        if models and model_id not in models:
            continue
        if model_id in settings.exclude_models:
            continue
        if "cycles" not in model_config or "fetch" not in model_config:
            continue
        due_runs = due_by_model.get(model_id, set())

        # Runs not due a fetch this tick still deserve a reclaim visit: their
        # frames may have been rendered by an earlier pass (or migrated from
        # the desktop, rollout step 3) and their raw is pure cost now.
        for run_init in _archived_run_inits(model_id):
            if run_init in due_runs or not already_fetched(model_id, run_init):
                continue
            # fetch=False: no download, but still extract-if-ready and
            # reclaim. That catches a run fetched before this pipeline
            # existed (rollout step 3's migrated renders) or one whose
            # extraction failed on an earlier pass.
            outcome = process_run(model_id, model_config, run_init, settings,
                                  apply=apply, now=now, fetch=False)
            if outcome.files_reclaimed or outcome.needs_attention or outcome.errors:
                result.outcomes.append(outcome)

    # Manifests describe the rendered tree, so they are regenerated whenever
    # rendering happened - dry-run means "delete nothing", not "do nothing".
    if rendered_anything:
        publish_activity("manifests", pass_started_at=_pass_started)
        result.manifests = render.regenerate_manifests()

    result.finished_at = datetime.now(UTC)
    result.free_bytes_after = free_bytes()
    publish_activity("sleeping", pass_started_at=_pass_started,
                     pass_finished_at=result.finished_at)
    return result


# --------------------------------------------------------------------------
# observability
# --------------------------------------------------------------------------

STATUS_FILENAME = "pipeline_status.json"
# A read-only --sweep used to overwrite the live status, so the dashboard would
# confidently describe a diagnostic dry run as the state of production. Dry runs
# get their own file.
DRYRUN_STATUS_FILENAME = "pipeline_status.dryrun.json"
ACTIVITY_FILENAME = "pipeline_activity.json"
HISTORY_FILENAME = "pipeline_history.jsonl"


def _out_dir():
    from src.viz import frame_renderer
    return frame_renderer.OUTPUT_DIR


EVENTS_FILENAME = "pipeline_events.jsonl"
EVENTS_MAX_ROWS = 6000
_events_lock = threading.Lock()
_last_activity_key: tuple | None = None


def append_event(payload: dict) -> None:
    """One line per thing that happened. Appended under a lock and trimmed in
    the same critical section: the coverage thread and the pass loop both write
    here, and a read-rewrite trim racing an append would lose lines."""
    import json

    try:
        path = _out_dir() / EVENTS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with _events_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
            rows = path.read_text(encoding="utf-8").splitlines()
            if len(rows) > EVENTS_MAX_ROWS:
                path.write_text("\n".join(rows[-EVENTS_MAX_ROWS:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def publish_activity(phase: str, **fields) -> None:
    """What the pipeline is doing RIGHT NOW.

    A pass takes minutes and writes its status only at the end, so without this
    a running pipeline and a wedged one look identical from outside - the exact
    ambiguity that made a stalled render worker hard to spot on the desktop.
    Written at model/run/window granularity, never per file, so it costs
    nothing measurable.
    """
    import json
    from datetime import UTC, datetime

    payload = {"phase": phase,
               "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
    for k, v in fields.items():
        payload[k] = v.isoformat().replace("+00:00", "Z") if hasattr(v, "isoformat") else v
    try:
        path = _out_dir() / ACTIVITY_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)   # atomic: a reader never sees a half-written file
    except OSError:
        pass

    # ...and append it to the feed, so the timeline shows the actual work
    # ("fetching gfs chunk 3/17") rather than only the 60 s coverage diffs,
    # which miss everything that happens between two scans.
    global _last_activity_key
    key = (phase, fields.get("model"), str(fields.get("run_init")), fields.get("chunk"))
    if key != _last_activity_key:
        _last_activity_key = key
        # Only fetch and render are worth a timeline line. "model" carries the
        # models-done counter and has no run at all; extracting and reclaiming
        # are sub-steps of a chunk that fire between every fetch/render pair
        # and tripled the feed's length without saying anything you cannot read
        # off the fetch/render lines around them. They still drive the live
        # panel and the busy indicator - they are just not events.
        if fields.get("model") and phase in ("fetching", "rendering"):
            append_event({"kind": "activity", "at": payload["updated_at"], "phase": phase,
                          "model": fields.get("model"),
                          "run_init": payload.get("run_init"),
                          "chunk": fields.get("chunk"), "chunks": fields.get("chunks"),
                          "window_cap_h": fields.get("window_cap_h")})


def append_history(result: PassResult) -> None:
    """One line per completed pass. Unlocks every trend on the dashboard -
    disk-free against the floor, GB reclaimed per pass, and pass duration
    against the interval, which is what shows the box falling behind BEFORE
    it stalls outright."""
    import json

    d = status_dict(result)
    row = {
        "at": d["finished_at"] or d["started_at"],
        "mode": d["mode"],
        "duration_s": None,
        "free_gb_before": d["free_gb_before"],
        "free_gb_after": d["free_gb_after"],
        "gb_reclaimed": d["gb_reclaimed"],
        "gb_held": d["gb_held"],
        "runs": len(d["runs"]),
        "steps_rendered": sum(r["steps_rendered"] or 0 for r in d["runs"]),
        "files_reclaimed": sum(r["files_reclaimed"] or 0 for r in d["runs"]),
        "n_errors": len(d["errors"]) + sum(len(r["errors"]) for r in d["runs"]),
        "n_attention": len(d["needs_attention"]),
    }
    if result.finished_at and result.started_at:
        row["duration_s"] = round((result.finished_at - result.started_at).total_seconds(), 1)
    try:
        path = _out_dir() / HISTORY_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def status_dict(result: PassResult, settings=None) -> dict:
    return {
        # The dashboard draws the disk floor as a fixed reference line; without
        # it "41 GB free" is a number with no meaning attached.
        "min_free_gb": getattr(settings, "min_free_gb", None),
        "started_at": _iso_z(result.started_at),
        "finished_at": _iso_z(result.finished_at) if result.finished_at else None,
        "mode": "apply" if result.applied else "dry-run",
        "free_gb_before": round(result.free_bytes_before / 1024**3, 2),
        "free_gb_after": round(result.free_bytes_after / 1024**3, 2),
        "gb_reclaimed": round(result.bytes_reclaimed / 1024**3, 3),
        "gb_held": round(result.bytes_held / 1024**3, 3),
        "needs_attention": result.needs_attention,
        "errors": result.errors,
        "manifests_regenerated": result.manifests,
        "runs": [
            {
                "model": o.model,
                "run_init": _iso_z(o.run_init),
                "chunks": o.chunks,
                "steps_rendered": o.steps_rendered,
                "extracted_rows": o.extracted_rows,
                "files_reclaimed": o.files_reclaimed,
                "gb_reclaimed": round(o.bytes_reclaimed / 1024**3, 3),
                "gb_held": round(o.bytes_held / 1024**3, 3),
                "skipped": o.skipped,
                "errors": o.errors,
            }
            for o in result.outcomes
        ],
    }


def write_status(result: PassResult, settings=None) -> None:
    """Machine-readable pass status next to rendered_index.json /
    backfill_progress.json, so rollout step 5's status page has one more
    thing to read and a stalled pipeline is visible without shell access."""
    import json

    from src.viz import frame_renderer

    path = frame_renderer.OUTPUT_DIR / (
        STATUS_FILENAME if result.applied else DRYRUN_STATUS_FILENAME)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status_dict(result, settings), indent=2), encoding="utf-8")
    except OSError as e:
        # A dry-run against a read-only mount is a legitimate way to inspect
        # a production box; not being able to write the status file must not
        # turn that into a failed pass.
        log.warning("could not write %s: %s", path, e)


def ping_healthcheck(suffix: str = "") -> None:
    """Deadman's-switch ping (CLAUDE.md: every scheduled fetch pings a
    healthcheck URL). healthchecks.io semantics: `/start` when a pass begins,
    bare URL on success, `/fail` when it raised - so a pass that hangs
    halfway shows up as a started-but-never-finished check rather than as
    silence, which is the failure mode that matters during Aug 5-12."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        httpx.get(url.rstrip("/") + suffix, timeout=10)
    except Exception as e:  # noqa: BLE001 - a dead healthcheck must never kill a pass
        log.warning("healthcheck ping failed: %s", e)

