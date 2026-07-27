import logging
import os
import subprocess
import sys
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

RENDER_WORKER_FLAG = "--render-worker"
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
    log.info("render worker started, %d renderable model(s)", len(renderable))

    while True:
        pending = _runs_needing_render(renderable)
        if not pending:
            time.sleep(RENDER_IDLE_SLEEP_SECONDS)
            continue
        rendered_any = False
        for model_id, run_init, raw_count in pending:
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
            n_steps = len(result)
            n_with_data = sum(1 for fields in result.values() if any(fields.values()))
            _record_render(model_id, run_init, raw_count)
            rendered_any = True
            log.info(
                "rendered %s %s: %d step(s), %d with real data",
                model_id, run_init.isoformat(), n_steps, n_with_data,
            )
            # New frames are useless to the tools until the manifests describe
            # them, so publish as we go rather than only at the end of what may
            # be an hours-long catch-up sweep.
            regenerate_manifests()
        if rendered_any:
            # Unconditional final pass: the rate limiter may have skipped the
            # last run's regeneration, and that is exactly the one the tools
            # need.
            regenerate_manifests(force=True)
        else:
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
MANIFEST_MIN_INTERVAL_S = 60.0
_last_manifest_regen = 0.0


def regenerate_manifests(force: bool = False) -> None:
    """Rebuild all three tool manifests from whatever frames are on disk.

    Never raises: a broken manifest generator must not stop rendering, and
    must certainly not stop fetching. Each generator is attempted
    independently so one failure does not stale the other two.

    The generators are imported lazily - they pull in the viz stack, which the
    archiver process itself has no business carrying (or failing to start on).
    """
    global _last_manifest_regen
    now = time.monotonic()
    if not force and (now - _last_manifest_regen) < MANIFEST_MIN_INTERVAL_S:
        return
    _last_manifest_regen = now

    try:
        from scripts import (
            generate_tool1_manifest,
            generate_tool2_manifest,
            generate_tool3_manifest,
        )
    except Exception:
        log.exception("manifest generators unavailable - frames rendered, manifests NOT updated")
        return

    for module in (generate_tool1_manifest, generate_tool2_manifest, generate_tool3_manifest):
        try:
            module.main()
        except Exception:
            log.exception("%s.main() failed - that manifest is now stale", module.__name__)


_render_proc: subprocess.Popen | None = None


def ensure_render_worker() -> None:
    """Start the render child if it isn't running, restart it if it died.
    Called once per tick; costs a poll() when all is well. Deliberately does
    NOT wait for or communicate with the child - see the note above."""
    global _render_proc
    if not RENDER_IN_SCHEDULER:
        return
    if _render_proc is not None:
        if _render_proc.poll() is None:
            return
        log.error(
            "render worker exited with code %s - restarting it", _render_proc.returncode
        )
    _render_proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-m", "src.scheduler.run", RENDER_WORKER_FLAG]
    )
    log.info("render worker running as pid %d", _render_proc.pid)


def run_once() -> None:
    models = load_models()["models"]
    now = datetime.now(UTC)
    for model_name, model_config in models.items():
        if "cycles" not in model_config or "fetch" not in model_config:
            continue  # aggregator/reference entries (open_meteo, climatology): no direct fetch
        for run_init in cycle_run_inits(model_config["cycles"], now):
            already_have_files = already_fetched(model_name, run_init)
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
                    record_fetch_attempt(model_name, run_init, now)
                    result = fetcher(model_name, model_config, run_init)
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

            # Extract whenever files exist and haven't been extracted yet - covers
            # both a fresh fetch just above AND a run fetched on an earlier tick
            # whose extraction failed or was never attempted (e.g. this module
            # was added after the fetch already happened).
            if not already_have_files or already_extracted(model_name, run_init):
                continue
            try:
                extractor = extract_registry.get_extractor(model_config["fetch"])
                rows = extractor(model_name, model_config, run_init)
                append_points(rows)
                mark_extracted(model_name, run_init)
                log.info(
                    "extracted %s %s: %d points.parquet rows",
                    model_name,
                    run_init.isoformat(),
                    len(rows),
                )
            except Exception as e:
                log.error("extract failed for %s %s: %s", model_name, run_init.isoformat(), e)
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
    while True:
        # First thing in the tick, and never waited on: (re)starting the render
        # child is a poll()+maybe-a-spawn, so fetching is never behind it.
        ensure_render_worker()
        try:
            run_once()
        except Exception:
            log.exception("run_once() failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    # Same module, two roles - see the automatic-rendering note above for why
    # rendering is a child process rather than part of the archiver's own.
    if RENDER_WORKER_FLAG in sys.argv[1:]:
        render_worker_main()
    else:
        main()
