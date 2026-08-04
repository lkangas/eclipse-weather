import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src.config import DATA_RAW, load_models
from src.extract import registry as extract_registry
from src.extract.base import already_extracted, append_points, mark_extracted
from src.fetchers import registry as fetch_registry
from src.fetchers.base import (
    already_fetched,
    cycle_run_inits,
    due_time,
    format_init_dir,
    raw_data_files,
    record_fetch_attempt,
    should_attempt_fetch,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")

CHECK_INTERVAL_SECONDS = 300  # 5 minutes

# aemet_harmonie is deliberately excluded from points.parquet entirely
# (explicit user direction, 2026-08-04): its cloud_total is already the
# weakest signal in the registry (no L/M/H, approximate colour-ramp-legend
# inversion rather than a real numeric field - see aemet_extractor.py's own
# docstring), and its temperature would need the same lossy legend-inversion
# treatment on top, which isn't worth building. Raw GeoTIFF fetch and map
# rendering are UNAFFECTED - this only skips the points.parquet extraction
# step.
_EXTRACTION_EXCLUDED_MODELS = {"aemet_harmonie"}


def ping_healthcheck() -> None:
    """Deadman's-switch style ping — CLAUDE.md: 'every scheduled fetch pings a
    healthcheck URL'. Pinged every loop iteration regardless of whether anything
    was due, so a crashed/stuck scheduler shows up as a missed ping, not silence."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        httpx.get(url, timeout=10)
    except Exception as e:
        log.warning("healthcheck ping failed: %s", e)


# ---------------------------------------------------------------------------
# Automatic rendering
#
# Production's shape is fetch -> render everything -> delete raw (CLAUDE.md's
# disk-footprint note), so rendering cannot keep being something a human
# remembers to run. But fetching is the irreplaceable half of this process - a
# missed run is unrecoverable (hard constraint #1) while a missed render is
# always redoable from raw data - so rendering must not be able to slow, block
# or crash the fetch loop. That rules out doing it inline in run_once() (a
# full render_run() on aifs_ens takes ~10 minutes against a 300s loop
# interval, and it would sit between one model's fetch and the next model's),
# and it also rules out a background THREAD in this process:
#
#   - eccodes/cfgrib is not thread-safe, and run_once() is already using
#     cfgrib in this thread for extraction. Concurrent use from a render
#     thread risks taking the whole archiver down with a native crash.
#   - matplotlib's pyplot state is process-global.
#   - a render of a 50-member aifs_ens run is the most memory-hungry thing
#     this codebase does; an OOM there would kill the archiver too.
#
# So rendering runs in a SEPARATE, SUPERVISED CHILD PROCESS (this same module,
# re-invoked with --render-worker). It cannot hold the GIL, cannot corrupt
# eccodes state, and cannot take the archiver down when it dies. The loop's
# only per-tick cost is one poll() on the child.
#
# There is deliberately NO ipc between them: the child discovers work by
# scanning data/raw, exactly the way the rest of this project coordinates
# stages (.last_fetch_attempt, the extraction marker). That also means it
# picks up runs fetched before this feature existed, and that a parent restart
# loses nothing.
#
# NOT implemented here, on purpose: deleting raw data after a successful
# render. That is a separate decision (see CLAUDE.md/T25).
# ---------------------------------------------------------------------------

# Set ECLIPSE_SCHEDULER_RENDER=0 to fetch/extract only - e.g. while driving
# scripts/render_backfill.py by hand, since two renderers writing the same
# frame path could race and leave a half-written PNG (render_frame() treats
# any existing file as done, so a torn PNG would never be redrawn).
RENDER_IN_SCHEDULER = os.environ.get("ECLIPSE_SCHEDULER_RENDER", "1") != "0"

RENDER_IDLE_SLEEP_SECONDS = 120  # nothing to render: how long before rescanning disk

# Raw-file count at this run's last completed render. A dotfile, so
# raw_data_files() ignores it (as it ignores the .idx sidecars cfgrib writes
# when a GRIB is read) - if the marker or the sidecars were counted, rendering
# a run would change its own count and re-trigger itself forever.
_RENDER_MARKER = ".last_render"


def _render_marker_path(model_id: str, run_init: datetime) -> Path:
    return DATA_RAW / model_id / format_init_dir(run_init) / _RENDER_MARKER


def _rendered_raw_count(model_id: str, run_init: datetime) -> int | None:
    try:
        return int(_render_marker_path(model_id, run_init).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _record_render(model_id: str, run_init: datetime, raw_count: int) -> None:
    marker = _render_marker_path(model_id, run_init)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(raw_count), encoding="utf-8")


def _parse_run_init(dirname: str) -> datetime | None:
    try:
        return datetime.strptime(dirname, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _runs_needing_render(renderable: set[str]) -> list[tuple[str, datetime, int]]:
    """Every archived (model, run_init, raw_file_count) whose raw directory has
    changed since it was last rendered - i.e. never rendered, or rendered and
    then gained steps (NOAA publishes gefs_extended's 385-840h range ~25-27h
    after init, long after the run first appeared on disk; treating "rendered
    once" as "rendered" is precisely the freeze this whole change is about).

    Ordered by RANK, not chronologically: every model's newest outstanding run
    first, then every model's second-newest, and so on - same reasoning as
    scripts/render_backfill.py's own _interleave_by_rank(), so a high-cadence
    model (arome_france, 8 cycles/day) cannot monopolise the queue ahead of a
    slower model's freshest run.
    """
    ranked: list[tuple[int, datetime, str, int]] = []
    for model_id in sorted(renderable):
        model_dir = DATA_RAW / model_id
        if not model_dir.exists():
            continue
        run_inits = sorted(
            (r for r in (_parse_run_init(p.name) for p in model_dir.iterdir() if p.is_dir())
             if r is not None),
            reverse=True,
        )
        rank = 0
        for run_init in run_inits:
            raw_count = len(raw_data_files(model_id, run_init))
            if raw_count == 0:
                continue  # nothing fetched (or an empty stray dir) - nothing to draw
            if _rendered_raw_count(model_id, run_init) == raw_count:
                continue
            ranked.append((rank, run_init, model_id, raw_count))
            rank += 1
    ranked.sort(key=lambda item: (item[0], -item[1].timestamp()))
    return [(model_id, run_init, raw_count) for _, run_init, model_id, raw_count in ranked]


# What the worker is doing RIGHT NOW. rendered_index.json is a disk scan and
# rendered_history.jsonl is a totals series - neither can express "currently
# rendering icon_global 2026-07-26 12Z, 40 s in", which is the first thing
# anyone looks for when they think a backfill has stalled. Written on entering
# and leaving each run, so a reader can also tell a long render from a wedged
# one by how old started_at is.
WORKER_ID = 0  # set from argv in __main__; each render worker gets its own file


def _live_status_path():
    from src.viz.frame_renderer import OUTPUT_DIR
    return OUTPUT_DIR / f"render_worker_status_{WORKER_ID}.json"


# Runs are claimed rather than divided up in advance. Sharding by model was the
# obvious alternative and is worse: a GFS run is 209 steps against AROME's 52,
# so fixed lanes leave workers idle on a queue that still has work in it.
# Claiming keeps every worker busy AND preserves the global newest-first
# ordering, since all of them walk the same ranked list and simply skip what is
# already taken.
CLAIM_STALE_SECONDS = float(os.environ.get("RENDER_CLAIM_STALE_S", "3600"))


def _claim_path(model_id: str, run_init: datetime):
    from src.viz.frame_renderer import OUTPUT_DIR
    # NOT under data/raw/: raw_data_files() counts everything in a run's
    # directory, so a lock file there would inflate raw_count and make the run
    # look permanently un-rendered.
    d = OUTPUT_DIR / ".claims"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{model_id}_{run_init:%Y%m%d%H}.json"


def _owner_is_live_worker(pid: int) -> bool:
    """Is `pid` still a running render worker?

    Checking the CMDLINE, not just liveness: pids get reused, and a recycled
    pid that happens to belong to some unrelated process would otherwise keep a
    dead worker's claim alive forever, stranding that run permanently.
    """
    if pid <= 0:
        return False
    try:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return False  # no such process
    return RENDER_WORKER_FLAG in cmdline.replace("\0", " ")


def _write_claim(path) -> bool:
    """O_EXCL create - the atomicity that makes this safe without a lock server.
    All render workers are children of one scheduler, so they share a PID
    namespace and can check each other's liveness directly."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        json.dump({"pid": os.getpid(), "worker": WORKER_ID,
                   "at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}, fh)
    return True


def _try_claim(model_id: str, run_init: datetime) -> bool:
    path = _claim_path(model_id, run_init)
    if _write_claim(path):
        return True
    # Someone holds it. Take it over ONLY if that someone is gone - a worker
    # SIGKILLed mid-render leaves its claim behind, and without this the run
    # would never be retried.
    #
    # Age must NOT be part of that test. It was, joined by `and`, and the
    # result was that a worker still rendering had its claim stolen the moment
    # it passed the staleness threshold: observed live with two workers on
    # arpege_europe 2026-07-23T00Z, one 147 min in and one 44 min in, which is
    # precisely the concurrent-writer case claims exist to prevent. Slow is not
    # dead. A 50-member ensemble on a cold NTFS mount can legitimately take
    # hours, and there is no upper bound worth guessing at.
    #
    # Age survives only as a fallback for a claim whose owner cannot be
    # determined at all (corrupt or truncated file) - otherwise such a claim
    # would strand its run forever.
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        owner = int(info.get("pid", -1))
        readable = True
    except (OSError, ValueError, TypeError):
        owner, readable = -1, False
    age = time.time() - path.stat().st_mtime if path.exists() else CLAIM_STALE_SECONDS + 1

    if readable:
        if _owner_is_live_worker(owner):
            return False          # alive and working - hands off, however slow
    elif age < CLAIM_STALE_SECONDS:
        return False              # unreadable but recent; give it time to settle
    log.warning("stealing render claim for %s %s - owner pid %s is not a live "
                "worker (claim %.0fs old)", model_id, run_init.isoformat(), owner, age)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return _write_claim(path)


def _release_claim(model_id: str, run_init: datetime) -> None:
    try:
        _claim_path(model_id, run_init).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not release render claim for %s %s", model_id, run_init.isoformat())


def _publish_live(current: dict | None, last: dict | None, pending: int | None = None) -> None:
    try:
        path = _live_status_path()
        prev = {}
        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prev = {}
        payload = {
            "worker": WORKER_ID,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "current": current,
            "last_completed": last if last is not None else prev.get("last_completed"),
            "pending_runs": pending if pending is not None else prev.get("pending_runs"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:  # never let status reporting break rendering
        log.exception("could not publish render worker status")


def render_worker_main() -> None:
    """Entry point for the child process (see the note above). Renders every
    archived run that needs it, forever, one at a time. Never exits on a
    per-run failure - a model whose reader is broken must not stop the other
    nine from rendering."""
    from src.viz.frame_renderer import _MODEL_READERS, render_run

    # _MODEL_READERS rather than a second hardcoded list of renderable models
    # here: the reader registry is what actually determines whether a model can
    # be rendered at all, and a copy of it in this file would silently rot the
    # first time a model is added there (CLAUDE.md hard constraint #2's spirit).
    renderable = set(_MODEL_READERS)
    log.info("render worker %d started, %d renderable model(s)", WORKER_ID, len(renderable))

    while True:
        # last_completed/pending_runs are carried forward by _publish_live when
        # passed None, so the page keeps showing them while the scan runs.
        _publish_live({"phase": "scanning",
                       "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")},
                      None)
        pending = _runs_needing_render(renderable)
        if not pending:
            _publish_live(None, None, pending=0)
            time.sleep(RENDER_IDLE_SLEEP_SECONDS)
            continue
        rendered_any = False
        for model_id, run_init, raw_count in pending:
            # Another worker is already on this one - skip, do not wait.
            if not _try_claim(model_id, run_init):
                continue
            started = datetime.now(UTC)
            _publish_live(
                {"model": model_id, "run_init": run_init.isoformat().replace("+00:00", "Z"),
                 "started_at": started.isoformat().replace("+00:00", "Z")},
                None, pending=len(pending),
            )
            # raw_count was sampled BEFORE this render: files landing while it
            # runs must still leave the run looking "grown" afterwards, or
            # those steps would wait for the next arrival to be noticed.
            try:
                result = render_run(model_id, run_init)
            except Exception:
                log.exception(
                    "render failed for %s %s", model_id, run_init.isoformat()
                )
                continue
            finally:
                _release_claim(model_id, run_init)
            n_steps = len(result)
            n_with_data = sum(1 for fields in result.values() if any(fields.values()))
            _record_render(model_id, run_init, raw_count)
            rendered_any = True
            # current stays on this run until the next iteration overwrites it,
            # a fraction of a second later. There is no between-runs work left
            # to report: manifests moved to their own process.
            _publish_live(
                None,
                {
                    "model": model_id,
                    "run_init": run_init.isoformat().replace("+00:00", "Z"),
                    "steps": n_steps,
                    "steps_with_data": n_with_data,
                    "seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
                    "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
            log.info(
                "rendered %s %s: %d step(s), %d with real data",
                model_id, run_init.isoformat(), n_steps, n_with_data,
            )
        if not rendered_any:
            # Every run in the sweep raised. A failed run records no marker, so
            # it is still pending on the next scan - without this sleep a
            # permanently-broken run (or model) would be retried as fast as the
            # exceptions can be raised, burning a core and flooding the log.
            time.sleep(RENDER_IDLE_SLEEP_SECONDS)


# Rendered frames are only half the story - the three tool pages read a
# manifest.json, not the frame directory, so a manifest that is not
# regenerated means the tools quietly keep serving old data even though the
# new frames are sitting right there on disk ("why doesn't tool 3 show the
# eclipse?" was exactly this: the frames existed, the manifest was an hour
# old). All three generators are now pure disk scans - tool1 ~0.5s, tool2
# ~5s, tool3 ~7s - so regenerating all three after a render is cheap enough
# to just do unconditionally rather than trying to work out which tool cares
# about which frame.
#
# Rate-limited rather than run per frame or per step: a render pass over the
# initial backlog would otherwise spend a meaningful slice of its time
# rescanning frame directories. In steady state a render takes far longer
# than this interval, so in practice every rendered run does get its own
# regeneration; the limit only collapses a burst of trivially fast ones.
# Regenerating all three manifests rescans every frame on disk and measured
# ~45s against the current archive - about 3x the cost of rendering the run
# that triggered it. At a 60s floor stamped BEFORE the work, a 17s render was
# enough to clear the interval again, so a catch-up sweep spent ~73% of its
# time republishing manifests nobody was reading yet. The floor is now stamped
# after completion (so it means "gap between regens", not "gap between
# starts") and defaults to 10 minutes: during a long backfill the tools go at
# most that long without seeing new frames, and the sweep runs ~3x faster.
MANIFEST_MIN_INTERVAL_S = float(os.environ.get("MANIFEST_MIN_INTERVAL_S", "600"))
_last_manifest_regen = 0.0


def regenerate_manifests(force: bool = False) -> None:
    """Rebuild Tool 1/2/3's manifests (from the rendered frames) and Tool 4's
    data (from points.parquet) so every tool stays current.

    Never raises: a broken manifest generator must not stop rendering, and
    must certainly not stop fetching. Each generator is attempted
    independently so one failure does not stale the others.

    The generators are imported lazily - they pull in the viz stack, which the
    archiver process itself has no business carrying (or failing to start on).
    """
    global _last_manifest_regen
    now = time.monotonic()
    if not force and (now - _last_manifest_regen) < MANIFEST_MIN_INTERVAL_S:
        return

    try:
        from scripts import (
            generate_tool1_manifest,
            generate_tool2_manifest,
            generate_tool3_manifest,
            generate_tool4_data,
        )
    except Exception:
        log.exception("manifest generators unavailable - frames rendered, manifests NOT updated")
        return

    for module in (generate_tool1_manifest, generate_tool2_manifest,
                   generate_tool3_manifest, generate_tool4_data):
        try:
            module.main()
        except Exception:
            log.exception("%s.main() failed - that manifest is now stale", module.__name__)
    _last_manifest_regen = time.monotonic()


RENDER_WORKER_FLAG = "--render-worker"
MANIFEST_WORKER_FLAG = "--manifest-worker"

# Each worker is a separate PROCESS, not a thread: eccodes and matplotlib are
# the reason - neither survives being driven concurrently from one interpreter.
# Separate processes also mean one worker wedging on a bad GRIB cannot stall the
# others, which is the whole point of the split.
RENDER_WORKERS = int(os.environ.get("RENDER_WORKERS", "4"))

def _worker_specs() -> dict[str, list[str]]:
    """label -> argv tail. One manifest worker, RENDER_WORKERS render workers.
    Render workers are interchangeable: they all walk the same ranked queue and
    claim what is free, so the count is a pure throughput dial."""
    specs = {"manifest-worker": [MANIFEST_WORKER_FLAG]}
    for i in range(RENDER_WORKERS):
        specs[f"render-worker-{i}"] = [RENDER_WORKER_FLAG, "--worker-id", str(i)]
    return specs


_WORKERS: dict[str, list] = {label: [None] for label in _worker_specs()}
# poll-then-spawn must be atomic: supervision and startup both call
# ensure_workers(), and a death noticed by both would otherwise start two of the
# same worker. Two renderers writing the same frame paths is the one thing
# frame_renderer cannot survive - a torn PNG from a concurrent savefig counts as
# rendered forever, since a frame's existence is what "has data" means now.
_worker_lock = threading.Lock()
SUPERVISE_SECONDS = 20


def ensure_workers() -> None:
    """Start each child that isn't running; restart any that died. Costs one
    poll() per worker when all is well."""
    if not RENDER_IN_SCHEDULER:
        return
    specs = _worker_specs()
    with _worker_lock:
        for label, slot in _WORKERS.items():
            proc = slot[0]
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                log.error("%s exited with code %s - restarting it", label, proc.returncode)
            slot[0] = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
                [sys.executable, "-m", "src.scheduler.run", *specs[label]]
            )
            log.info("%s running as pid %d", label, slot[0].pid)


def _supervise_workers_forever() -> None:
    """Keep the children alive on their OWN cadence, independent of the fetch
    loop.

    Recovery used to run once per tick, at the top of a loop whose body is
    run_once(), so a worker that died early in a tick stayed dead until that
    tick's fetching finished - 20+ minutes when the tick is pulling a 50-member
    ensemble. Observed exactly that. A thread is safe here where it is not for
    the work itself: this only calls poll()/Popen and touches no eccodes or
    matplotlib state.
    """
    while True:
        try:
            ensure_workers()
        except Exception:
            log.exception("worker supervision failed; will retry")
        time.sleep(SUPERVISE_SECONDS)


MANIFEST_INTERVAL_S = float(os.environ.get("MANIFEST_INTERVAL_S", "300"))


def manifest_worker_main() -> None:
    """Rebuild the tool manifests on a fixed cadence, forever, in its own
    process.

    This used to run inline in the render worker after every run, and it
    dominated it: rendering a 209-step GFS run measured 17s while regenerating
    the three manifests measured ~45s, because each generator rescans every
    frame on disk. A backfill therefore spent most of its time republishing
    manifests instead of rendering - and the rate limiter meant to contain that
    stamped its clock before the work, so a render was enough to clear the
    interval and trigger the next regeneration anyway.

    Manifests are pure derived state: they describe whatever frames happen to be
    on disk at the moment they run. Nothing about rendering depends on them, so
    there is no ordering to preserve and no reason for either job to wait on the
    other. Being a tick behind costs a tool one cycle of freshness; blocking the
    backfill costs hours.
    """
    log.info("manifest worker started, regenerating every %.0fs", MANIFEST_INTERVAL_S)
    while True:
        try:
            regenerate_manifests(force=True)
        except Exception:
            log.exception("manifest regeneration failed; will retry")
        time.sleep(MANIFEST_INTERVAL_S)


def run_once() -> None:
    models = load_models()["models"]
    now = datetime.now(UTC)
    for model_name, model_config in models.items():
        if "cycles" not in model_config or "fetch" not in model_config:
            continue  # aggregator/reference entries (open_meteo, climatology): no direct fetch
        for run_init in cycle_run_inits(model_config["cycles"], now):
            already_have_files = already_fetched(model_name, run_init)
            # The init the data actually landed under, which is not always the
            # one asked for: aemet_harmonie's endpoint serves whichever run is
            # current no matter what the caller requests, and its fetcher files
            # the bundle under the init the bundle itself declares, returning
            # that init in FetchResult.run_init (see aemet_geotiff_fetcher's
            # docstring, consequence 1). Every other fetcher returns the
            # requested init unchanged, so this is a no-op for them.
            fetched_init = run_init
            # Not just "have we fetched this run at all" - a run keeps gaining
            # steps after its first fetch (see should_attempt_fetch's note on
            # GEFS's extended range publishing ~25-27h after init), so it stays
            # eligible for top-up passes until it ages out. Fetchers skip files
            # they already hold, so a pass over a complete run is nearly free.
            if should_attempt_fetch(model_name, run_init, now):
                due = due_time(model_config.get("publication_lag_h", [0, 0]), run_init)
                if now < due:
                    continue
                try:
                    fetcher = fetch_registry.get_fetcher(model_config["fetch"])
                    # Stamped against the REQUESTED run: this marker is the
                    # per-run fetch throttle, and what was throttled is the
                    # request, whatever run came back.
                    record_fetch_attempt(model_name, run_init, now)
                    result = fetcher(model_name, model_config, run_init)
                    if result.run_init is not None and result.run_init != run_init:
                        log.info(
                            "%s: asked for %s but the fetcher delivered the %s run - "
                            "extraction follows the data",
                            model_name, run_init.isoformat(), result.run_init.isoformat(),
                        )
                        fetched_init = result.run_init
                        already_have_files = already_fetched(model_name, fetched_init)
                    n_new = len(result.files_written)
                    if already_have_files:
                        log.info(
                            "top-up %s %s: %s, %d file(s) present/written",
                            model_name, run_init.isoformat(), result.status, n_new,
                        )
                    else:
                        log.info(
                            "fetched %s %s: %s", model_name, run_init.isoformat(), result.status,
                        )
                    already_have_files = already_have_files or bool(result.files_written)
                except Exception as e:
                    log.error("fetch failed for %s %s: %s", model_name, run_init.isoformat(), e)
                    continue

            if model_name in _EXTRACTION_EXCLUDED_MODELS:
                continue
            if already_extracted(model_name, fetched_init):
                continue  # finalized (see below) - never re-extract
            # raw_data_files(), not already_fetched(): the latter counts this
            # module's own dot-markers, so a run whose directory holds nothing
            # but a .last_fetch_attempt looked "fetched" but has no real data.
            if not raw_data_files(model_name, fetched_init):
                continue
            # Incremental, freshness-first extraction - mirrors the production
            # pipeline (src/pipeline/orchestrator._maybe_extract). Extract as
            # soon as any step is on disk, re-extract whenever HIGHER steps have
            # arrived since last time (tracked in .extract_maxstep), and only
            # finalize (.extracted, which stops re-extraction) once every
            # published step has been seen OR the run is sealed (past the top-up
            # window). Without this, a run extracted while still publishing
            # (e.g. AROME's staggered SP2 windows land over hours) stays frozen
            # at its partial extent forever. mark_extracted only ever fires on a
            # run that actually produced rows, so a 0-row run is retried later.
            from src.pipeline import journal, verify
            v = verify.verify_run(model_name, model_config, fetched_init)
            on_disk_max = max(v.steps_on_disk) if v.steps_on_disk else -1
            marker = journal.run_dir(model_name, fetched_init) / ".extract_maxstep"
            try:
                last_max = int(marker.read_text())
            except (OSError, ValueError):
                last_max = -2
            complete = set(v.published_steps).issubset(
                set(v.steps_on_disk) | set(v.reclaimed_steps))
            final = v.sealed or (bool(v.published_steps) and complete)
            if on_disk_max <= last_max and not final:
                continue  # nothing new since last extraction, run not ready to finalize
            try:
                extractor = extract_registry.get_extractor(model_config["fetch"])
                rows = extractor(model_name, model_config, fetched_init)
                append_points(rows)
                if rows:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text(str(on_disk_max))
                    if final:
                        mark_extracted(model_name, fetched_init)
                log.info(
                    "extracted %s %s: %d rows through +%dh%s",
                    model_name, fetched_init.isoformat(), len(rows), on_disk_max,
                    " (final)" if (final and rows) else "",
                )
            except Exception as e:
                log.error("extract failed for %s %s: %s", model_name, fetched_init.isoformat(), e)
    ping_healthcheck()


def main() -> None:
    # Import fetcher/extractor submodules here (not at module load) purely
    # for their @register(...) side-effects.
    from src import extract, fetchers  # noqa: F401

    t = os.environ.get("ECLIPSE_T", "default")
    log.info("eclipse-weather archiver starting, ECLIPSE_T=%s", t)
    log.info(
        "in-scheduler rendering %s",
        "enabled" if RENDER_IN_SCHEDULER else "disabled (ECLIPSE_SCHEDULER_RENDER=0)",
    )
    # Supervision runs on its own thread so a dead worker is noticed within
    # SUPERVISE_SECONDS regardless of how long the current fetch takes.
    if RENDER_IN_SCHEDULER:
        ensure_workers()
        threading.Thread(
            target=_supervise_workers_forever, name="worker-supervisor", daemon=True
        ).start()

    while True:
        try:
            run_once()
        except Exception:
            log.exception("run_once() failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    # Same module, two roles - see the automatic-rendering note above for why
    # rendering is a child process rather than part of the archiver's own.
    # One module, three roles. Fetching, rendering and manifest generation each
    # run as their own process so none of them can block the others.
    if RENDER_WORKER_FLAG in sys.argv[1:]:
        if "--worker-id" in sys.argv:
            WORKER_ID = int(sys.argv[sys.argv.index("--worker-id") + 1])
        render_worker_main()
    elif MANIFEST_WORKER_FLAG in sys.argv[1:]:
        manifest_worker_main()
    else:
        main()
