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
    record_fetch_attempt(model_id, run_init, now)

    # Steps upstream has repeatedly refused to serve. Narrowing here rather
    # than inside the fetcher keeps src/fetchers/ - the desktop archiver's
    # critical path - completely untouched by this.
    skip_steps = failures.dead_steps(model_id, run_init, now)

    previous_cap: int | None = None
    for cap in caps:
        wanted = chunking.steps_in_chunk(model_config, run_init, cap, previous_cap)
        previous_cap = cap
        if skip_steps:
            dropped = [s for s in wanted if s in skip_steps]
            wanted = [s for s in wanted if s not in skip_steps]
            if dropped:
                log.info("%s %s window <=+%dh: skipping %d step(s) upstream keeps "
                         "refusing: %s", model_id, run_init.isoformat(), cap,
                         len(dropped), dropped[:6])
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
            failures.record(model_id, run_init, result.error, now)
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

    for model_id, model_config in all_models.items():
        if models and model_id not in models:
            continue
        if "cycles" not in model_config or "fetch" not in model_config:
            continue  # aggregator/reference entries carry no fetch path

        # Running totals so a pass in flight is visible as progress rather
        # than as nothing-until-it-finishes.
        publish_activity("model", model=model_id,
                         model_index=_fetchable.index(model_id) + 1,
                         models_total=len(_fetchable),
                         pass_started_at=_pass_started,
                         runs_done=len(result.outcomes),
                         gb_reclaimed_so_far=round(result.bytes_reclaimed / 1024**3, 3),
                         errors_so_far=len(result.errors) + sum(
                             len(o.errors) for o in result.outcomes))

        due_runs = set()
        for run_init in cycle_run_inits(model_config["cycles"], now):
            if now < due_time(model_config.get("publication_lag_h", [0, 0]), run_init):
                continue
            if not should_attempt_fetch(model_id, run_init, now):
                continue
            due_runs.add(run_init)
            try:
                outcome = process_run(model_id, model_config, run_init, settings,
                                      apply=apply, now=now)
            except Exception as e:
                result.errors.append(f"{model_id} {run_init.isoformat()}: {e}")
                log.exception("process_run failed for %s %s", model_id, run_init.isoformat())
                continue
            rendered_anything = rendered_anything or bool(outcome.steps_rendered)
            result.outcomes.append(outcome)

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

