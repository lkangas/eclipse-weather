import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import DATA_RAW, eclipse_config, get_model

log = logging.getLogger(__name__)

DEFAULT_ECLIPSE_T = "2026-08-12T18:30:00Z"


def eclipse_t() -> datetime:
    """Read ECLIPSE_T from the environment (falls back to config/models.yaml's
    eclipse.t, then to DEFAULT_ECLIPSE_T). Never hardcode a date elsewhere —
    always go through this function so sim modes (T15/T16) work unmodified."""
    raw = os.environ.get("ECLIPSE_T")
    if not raw:
        raw = eclipse_config().get("t", DEFAULT_ECLIPSE_T)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def target_valid_times(archive_valid_hours_utc: list[int]) -> list[datetime]:
    """The archive valid times (e.g. 15/18/21 UTC) on eclipse_t()'s own calendar date."""
    t = eclipse_t()
    return [t.replace(hour=h, minute=0, second=0, microsecond=0) for h in archive_valid_hours_utc]


def format_init_dir(run_init: datetime) -> str:
    """Directory-name convention per CLAUDE.md repo layout: data/raw/{model}/{initYYYYMMDDHH}/"""
    return run_init.strftime("%Y%m%d%H")


def format_init_iso(run_init: datetime) -> str:
    """General init-time string convention per CLAUDE.md hard constraint #4: YYYYMMDDTHH."""
    return run_init.strftime("%Y%m%dT%H")


def raw_output_dir(model_name: str, run_init: datetime) -> Path:
    d = DATA_RAW / model_name / format_init_dir(run_init)
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_available_steps(steps_spec: list[dict]) -> list[int]:
    """Expand a models.yaml `steps:` spec (e.g. [{to_h:120,every_h:1},{to_h:384,every_h:3}])
    into the full list of forecast-hour offsets the model actually publishes."""
    steps = [0]
    prev_to_h = 0
    for seg in steps_spec:
        to_h, every_h = seg["to_h"], seg["every_h"]
        h = prev_to_h
        while True:
            h += every_h
            if h > to_h:
                break
            steps.append(h)
        prev_to_h = to_h
    return sorted(set(steps))


def nearest_step(
    available_steps: list[int], target_offset_hours: float,
    tolerance_h: float = 0.0,
) -> tuple[int, float] | None:
    """Nearest available forecast-hour step to a target offset. Returns
    (step, |misalignment_hours|), or None if the target is before init or
    beyond the model's max reach (run doesn't cover it yet).

    `tolerance_h` extends how far PAST the last step the target may sit and
    still resolve to that last step. It exists because the strict test makes
    the end of a run behave unlike the middle of it: inside the range this
    function happily returns a step up to half the cadence away from the
    target (a 6-hourly model can answer with a frame 3 h off), but one hour
    past the last step it refuses outright.

    Measured case, 2026-07-29: aifs_ens runs to +360 h, so its 07-28 18Z run
    ends at 2026-08-12 18:00Z - thirty minutes before the eclipse. GFS from
    the SAME init resolved to its own +360 and showed the very same 18:00Z
    frame, because GFS keeps going to +384 and 360.5 h therefore fell inside
    its range. Two identical pictures, one shown and one refused, decided by
    a step neither of them used.

    Default 0.0 keeps every existing caller bit-identical - in particular
    steps_for_run() and the fetchers/extractors built on it, whose targets
    are the whole-hour archive valid times where this never bit. Only a
    caller with a target BETWEEN steps (the tools' 18:30 eclipse moment)
    needs to opt in.
    """
    if target_offset_hours < 0:
        return None
    if target_offset_hours > max(available_steps) + tolerance_h:
        return None
    step = min(available_steps, key=lambda s: abs(s - target_offset_hours))
    return step, abs(step - target_offset_hours)


def end_of_range_tolerance_h(available_steps: list[int]) -> float:
    """Half the final step spacing - the tolerance that makes nearest_step()
    treat the end of a run the same way it treats the middle.

    Half, specifically, because that is already the worst misalignment the
    interior can produce: with steps every 6 h, any target lands within 3 h
    of one of them. Allowing the same 3 h past the last step admits exactly
    the runs that end just short of the target and refuses the ones that end
    genuinely short. 0.0 for a run with fewer than two steps, which has no
    spacing to halve.
    """
    if len(available_steps) < 2:
        return 0.0
    return (available_steps[-1] - available_steps[-2]) / 2


def _available_steps_for_cycle(model_config: dict, run_init: datetime) -> list[int]:
    """Every published forecast-hour step for this specific run_init's cycle,
    capped by that cycle's own max reach. `cycles:` gives a max forecast
    length PER CYCLE HOUR (e.g. gefs_extended's 00Z reaches 840h but
    06/12/18Z only reach 384h; ecmwf_hres and ukmo_global have similar
    splits) - this must additionally cap `steps:`'s shared cadence spec, or
    a short cycle gets asked for steps its run was never going to publish.

    `first_step_h` (models.yaml, default 0) drops steps the source never
    distributes at all, as opposed to a step that simply has not arrived
    YET. aemet_harmonie is the known case: its bundle starts at run_init+1h,
    the analysis hour is never in it, no request is ever made for it, and no
    fetch failure is ever recorded for it either - so completeness.py had no
    way to tell "permanently absent" from "not fetched yet" and declared
    every run incomplete forever. aemet_geotiff_fetcher.py already excluded
    step 0 from its OWN bundle-size check (`_expected_raster_count`) but
    that knowledge stayed local to the fetcher; every other consumer of
    full_range_steps() - completeness, coverage.py's dashboard, the tool
    manifests - still expected it. Filtering here is the one place all of
    them share, so `_expected_raster_count` now reuses this instead of
    re-stating "s > 0" a second time.
    """
    available = generate_available_steps(model_config["steps"])
    floor = model_config.get("first_step_h", 0)
    if floor:
        available = [s for s in available if s >= floor]
    cycle_max = model_config.get("cycles", {}).get(f"{run_init.hour:02d}")
    if cycle_max is not None:
        available = [s for s in available if s <= cycle_max]
    return available


def steps_for_run(model_config: dict, run_init: datetime) -> dict[str, tuple[int, float] | None]:
    """For each of the eclipse archive's target valid times, the (step, misalignment)
    this run_init/model can supply, or None if this run doesn't reach that valid time.
    """
    valid_hours = eclipse_config()["archive_valid_hours_utc"]
    available = _available_steps_for_cycle(model_config, run_init)
    result = {}
    for valid_time in target_valid_times(valid_hours):
        offset_hours = (valid_time - run_init).total_seconds() / 3600
        result[valid_time.isoformat()] = nearest_step(available, offset_hours)
    return result


def all_valid_times_for_run(model_config: dict, run_init: datetime) -> dict[str, tuple[int, float]]:
    """The full-range counterpart to steps_for_run(): every step this run
    actually publishes (up to data_horizon(), same cutoff full_range_steps()
    already uses for raw fetch + map rendering) mapped to ITS OWN real valid
    time, not just the 3 eclipse-day archive hours.

    Same (valid_time_iso -> (step, misalignment)) shape as steps_for_run() on
    purpose - every extractor call site that consumes steps_for_run() already
    discards the misalignment (each step's own valid time has none, by
    construction, so it's always 0.0 here) and only uses the dict shape to
    iterate (valid, step) pairs, so this is a drop-in replacement wherever an
    extractor should capture the whole run instead of just the archive hours.

    Added 2026-08-03 after a design review: the raw fetch has covered the
    full forecast range since the 2026-07-23 archiver consolidation, but
    point extraction into points.parquet was still narrowed to 3 hours/run,
    which meant nothing analogous to a full per-run forecast curve was ever
    queryable without re-reading raw GRIB by hand. points.parquet is tiny
    (measured 2026-08-03: 1.6 MB for 486k rows) - this doesn't come close to
    a real storage concern even at 40-60x more rows per run.
    """
    horizon = data_horizon()
    result = {}
    for step in _available_steps_for_cycle(model_config, run_init):
        valid_time = run_init + timedelta(hours=step)
        if valid_time > horizon:
            continue
        result[valid_time.isoformat()] = (step, 0.0)
    return result


def data_horizon() -> datetime:
    """Latest valid time worth having at all (eclipse.data_horizon in
    models.yaml). Beyond it the forecast is of no use to this project, so
    nothing fetches, renders or lists it.

    Independent of eclipse_t() ON PURPOSE - see the config comment. This is a
    fixed fact about the real project timeline; `t` is a UI/extraction target
    the sim modes move around, and deriving one from the other lets a
    time-shifted ECLIPSE_T silently stop the archiver dead.
    """
    raw = eclipse_config().get("data_horizon")
    if not raw:
        return datetime.max.replace(tzinfo=UTC)
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def full_range_steps(model_config: dict, run_init: datetime) -> list[int]:
    """Every step this run publishes with a valid time at or before
    data_horizon() - Tool 1's general-purpose "latest run of every model"
    explorer wants the whole forecast horizon a run actually publishes, not
    just the eclipse-day archive hours steps_for_run() targets, but nothing
    past the horizon is of use to anyone.

    This is the single step source shared by every fetcher, by
    frame_renderer.render_run(), and by the manifest scripts, so applying
    the cut here is what keeps all of them consistent - and is why the UI's
    extent slider automatically stops at the horizon too: it is derived from
    whatever steps the manifests actually contain.
    """
    horizon = data_horizon()
    return [
        h for h in _available_steps_for_cycle(model_config, run_init)
        if run_init + timedelta(hours=h) <= horizon
    ]


def latest_available_run_init(model_config: dict, now: datetime) -> datetime | None:
    """The most recent run_init that should actually be published by now
    (init time + this model's own publication_lag_h already elapsed) - Tool
    1 wants the true current state of each model, not a run whose data
    isn't out yet and would just 404."""
    candidates = cycle_run_inits(model_config["cycles"], now, lookback_hours=48)
    lag = model_config.get("publication_lag_h", [0])
    due = [c for c in candidates if due_time(lag, c) <= now]
    return due[-1] if due else None


def cycle_run_inits(cycles: dict, now: datetime, lookback_hours: int = 48) -> list[datetime]:
    """Every run_init for this model's cycles (e.g. {"00":384,"06":384,...}) that
    falls within the last lookback_hours of `now` — the scheduler's candidate set
    for 'should this run have been fetched by now'."""
    run_inits = []
    for cycle_hour_str in cycles:
        cycle_hour = int(cycle_hour_str)
        for days_back in range(0, (lookback_hours // 24) + 2):
            day = (now - timedelta(days=days_back)).date()
            candidate = datetime(day.year, day.month, day.day, cycle_hour, tzinfo=UTC)
            if candidate <= now and (now - candidate).total_seconds() / 3600 <= lookback_hours:
                run_inits.append(candidate)
    return sorted(run_inits)


def due_time(
    publication_lag_h: list[float], run_init: datetime, margin_minutes: int = 15
) -> datetime:
    """When this run_init should be considered fetchable: init + the conservative
    (upper-bound) publication lag + a small safety margin."""
    lag = publication_lag_h[1] if len(publication_lag_h) > 1 else publication_lag_h[0]
    return run_init + timedelta(hours=lag, minutes=margin_minutes)


def already_fetched(model_name: str, run_init: datetime) -> bool:
    """Cheap idempotency check: has anything been written for this run already?

    Deliberately counts the dot-markers below too, not just data files: a run
    whose every download failed still has a `.last_fetch_attempt`, and that is
    exactly what keeps should_attempt_fetch() on the rate-limited top-up path
    instead of re-requesting ~154 not-yet-published files every 5 minutes (the
    NOAA "Slow Down" case its own note describes). Use raw_data_files() when
    you want "did this run actually gain data".
    """
    d = DATA_RAW / model_name / format_init_dir(run_init)
    return d.exists() and any(d.iterdir())


# cfgrib/herbie drop a "<gribname>.<hash>.idx" sidecar next to a GRIB the
# first time that GRIB is OPENED (extract/render), not when it is fetched.
# Anything that counts files in a run directory to answer "did this run gain
# steps since last time" must exclude them, or merely reading a run looks
# like new data arriving and the reader re-triggers itself forever.
_SIDECAR_SUFFIXES = {".idx"}


def raw_data_files(model_name: str, run_init: datetime) -> list[Path]:
    """The actual downloaded data files in a run's raw directory - excluding
    this module's own dot-markers and the .idx sidecars described above.

    This is the "how much of this run do we actually have" signal, as opposed
    to already_fetched()'s "has anything at all been written". The scheduler
    uses the count to notice a run that gained steps after it was last
    rendered (see src/scheduler/run.py)."""
    d = DATA_RAW / model_name / format_init_dir(run_init)
    if not d.exists():
        return []
    return [
        p for p in d.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() not in _SIDECAR_SUFFIXES
    ]


# "The file exists" is not "the file is usable", and every fetcher's per-file
# idempotency check used to be a bare `dest_path.exists()`. A download that
# dies mid-transfer leaves a truncated (or zero-byte) GRIB behind, which then
# looks "already fetched" to every subsequent top-up pass, so the run stays
# broken permanently. The case that motivated this:
# ecmwf_hres/2026072612/tcc_f123.grib2 was written 0 bytes; cfgrib raises
# "EOFError: No valid message found" on it, that run rendered "65 steps, 41
# with real data", and it would have stayed that way forever.
#
# The check runs on every candidate file of every still-topping-up run, every
# tick, so it must be cheap - a real GRIB parse of ~23k archived files is not
# an option. It is therefore STRUCTURAL, not a parse: GRIB edition 1 and 2
# both start with the ASCII magic "GRIB" (section 0) and end with the ASCII
# sentinel "7777" (section 5 / section 8). Two seeks and 68 bytes per file
# catch every failure mode a broken download actually produces - empty file,
# body cut short, an HTML/XML error page saved under a .grib2 name.
#
# Measured false-positive risk: this exact test over all 22786 archived .grib2
# files flagged nothing but files that were genuinely not complete GRIBs.
# Deleting good archived data is the one unacceptable outcome, so the rule is
# narrow on purpose - only a file we ourselves named .grib/.grib2 is ever
# judged, and anything merely unrecognised is left strictly alone.
#
# What it deliberately does NOT catch: a file truncated exactly at a message
# boundary (say 3 of 6 requested messages), which is a structurally valid
# GRIB. Detecting that needs a per-model expected-message count, which is
# downstream knowledge and has no place in a hot idempotency check.
_GRIB_MAGIC = b"GRIB"
_GRIB_TRAILER = b"7777"
_GRIB_SUFFIXES = {".grib", ".grib2"}
_TRAILER_SEARCH_BYTES = 64  # slack for any trailing pad; see search-not-compare below

# A file still being written is structurally incomplete too, and that is the
# one false positive this test really has: the audit scan that produced the
# numbers above initially flagged two aifs_ens files as truncated, and they
# turned out to be 266 MB downloads in flight. Every fetcher writes straight
# to the destination path (no temp-then-rename), so a recently-touched file
# gets the benefit of the doubt and is reported as "leave it alone". Within
# one fetcher this changes nothing - its loop is sequential and can never be
# mid-writing the very path it is about to skip - but a second process (a
# hand-run fetch alongside the container) could be, and condemning its
# in-flight transfer would throw away a large download for no reason.
# Callers checking a download they JUST finished pass min_age_s=0.
_MID_WRITE_GRACE_S = 180.0


def _discard_broken(path: Path, reason: str) -> None:
    """Delete a file positively identified as broken so the fetcher's normal
    download path recreates it. Also drops any .idx sidecar built against the
    old bytes - a stale index pointing into a replaced file is worse than no
    index at all."""
    log.warning("discarding broken raw file %s (%s) - it will be re-fetched", path, reason)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.error("could not delete broken raw file %s: %s", path, e)
        return
    for sidecar in path.parent.glob(path.name + "*.idx"):
        sidecar.unlink(missing_ok=True)


def _is_reclaimed(path: Path) -> bool:
    """Whether production's reclaim pipeline deliberately deleted this file
    after verifying its frames. Imported lazily: src.pipeline is
    production-only and must never become a hard dependency of the desktop
    fetch path (the desktop never reclaims, so this is always False there)."""
    try:
        from src.pipeline.journal import is_reclaimed
    except ImportError:  # pipeline package not deployed - desktop/dev
        return False
    try:
        return is_reclaimed(path)
    except OSError:
        return False


def have_usable_file(dest_path: Path, min_age_s: float = _MID_WRITE_GRACE_S) -> bool:
    """Every fetcher's per-file idempotency check, in place of a bare
    `dest_path.exists()`. True means "don't download this now": the file is
    present and structurally complete, OR it is too recently written to judge
    (see _MID_WRITE_GRACE_S), OR it is a format this check has no opinion
    about. A file it does judge broken is DELETED, which turns it back into a
    plain missing file for the caller and lets the existing hourly top-up pass
    re-fetch it.

    Never deletes a file it merely does not recognise: anything non-empty that
    is not a .grib/.grib2 is reported usable and left untouched, and an
    unreadable file is left alone too (we cannot tell, so we do no harm).

    ALSO true for a file production deliberately deleted after verifying its
    frames were rendered (src/pipeline/journal.py's tombstone). That case is
    checked FIRST, before anything here can form an opinion about the file:
    a reclaimed file is absent BY DESIGN, and must never be mistaken for
    either a missing fetch or a corrupt one. Without it, the 48-hour top-up
    window would re-download, once an hour, every step production has
    already rendered and discarded - which is the whole point of reclaiming
    them. On the desktop nothing is ever reclaimed, no tombstone exists, and
    this reduces to the plain structural check.
    """
    if _is_reclaimed(dest_path):
        return True

    try:
        stat = dest_path.stat()
    except OSError:
        return False  # missing, or a path we cannot stat - treat as not fetched

    size = stat.st_size
    if min_age_s > 0 and (time.time() - stat.st_mtime) < min_age_s:
        return True  # possibly still being written - see _MID_WRITE_GRACE_S

    if size == 0:
        _discard_broken(dest_path, "zero bytes")
        return False

    if dest_path.suffix.lower() not in _GRIB_SUFFIXES:
        return True  # not a format this check understands - present is good enough

    try:
        with open(dest_path, "rb") as f:
            head = f.read(len(_GRIB_MAGIC))
            f.seek(-min(size, _TRAILER_SEARCH_BYTES), os.SEEK_END)
            tail = f.read()
    except OSError as e:
        log.warning("could not structurally check %s (%s) - leaving it alone", dest_path, e)
        return True

    # `in tail` rather than `tail == _GRIB_TRAILER`: a producer that pads the
    # end of a file would otherwise look corrupt. The extra tolerance costs
    # essentially nothing - the odds of a truncated GRIB's last 64 bytes of
    # packed data happening to contain the literal "7777" are ~1e-8.
    if head == _GRIB_MAGIC and _GRIB_TRAILER in tail:
        return True

    _discard_broken(
        dest_path, f"not a complete GRIB (starts {head!r}, no {_GRIB_TRAILER!r} at end)"
    )
    return False


# A run is not necessarily complete the first time it is fetched. Providers
# publish a run's steps progressively, and not always in step order: NOAA
# produces GEFS's 385-840h extended range member by member, with the control
# member (the only one this project fetches) near the END of that queue, so
# gefs_extended's 00Z run gains f390..f840 roughly 25-27h after init - long
# after its publication_lag_h of [4,6] triggers the first fetch. Treating
# "directory is non-empty" as "done" froze every such run at whatever existed
# at first fetch, permanently: that is why the 00Z runs on disk stop at f384
# and why the project had no data at the eclipse valid time. The same gate
# also stranded one-off casualties of a transient failure (an icon_global run
# left with zero files, an ecmwf_ens run with uneven product coverage).
#
# So a run stays eligible for top-up fetches until it ages out. Every fetcher
# is per-file idempotent (it checks the destination path before any network
# call), so a re-attempt on a COMPLETE run costs only stat()s - but on an
# incomplete one it re-requests each still-missing file, so attempts are
# rate-limited per run rather than run every scheduler tick. Without that,
# an incomplete gefs_extended 00Z run would re-request ~154 not-yet-published
# files every 5 minutes, which is what earns a 503 "Slow Down" from NOAA.
FETCH_RETRY_INTERVAL_H = 1.0
FETCH_TOPUP_WINDOW_H = 48.0  # covers the ~27h stagger with room to spare

_ATTEMPT_MARKER = ".last_fetch_attempt"


def _attempt_marker_path(model_name: str, run_init: datetime) -> Path:
    return DATA_RAW / model_name / format_init_dir(run_init) / _ATTEMPT_MARKER


def record_fetch_attempt(model_name: str, run_init: datetime, now: datetime | None = None) -> None:
    """Stamp that a fetch was just attempted for this run, so the next tick
    doesn't immediately retry it (see FETCH_RETRY_INTERVAL_H)."""
    marker = _attempt_marker_path(model_name, run_init)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text((now or datetime.now(UTC)).isoformat(), encoding="utf-8")


def _last_fetch_attempt(model_name: str, run_init: datetime) -> datetime | None:
    marker = _attempt_marker_path(model_name, run_init)
    try:
        return datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def should_attempt_fetch(model_name: str, run_init: datetime, now: datetime) -> bool:
    """Whether to (re)invoke the fetcher for this run now.

    Always true for a run with nothing on disk. For a run that already has
    files, true only while it is young enough to still be gaining steps and
    the last attempt is far enough behind - see the note above.

    The retry interval itself is `fetch_retry_interval_h` (models.yaml),
    defaulting to FETCH_RETRY_INTERVAL_H. Only worth tightening for a model
    whose re-attempt on an INCOMPLETE run stays cheap when most steps are
    still unpublished - the safety property the note above depends on. That
    is true of a model with a small, fixed request count regardless of how
    much has arrived (arome_france: 9 group windows x 2 packages = 18
    requests, worst case) or one gated behind a single cheap HEAD before any
    real download (aemet_harmonie). It is NOT true of a model that requests
    one file per (step, param) - icon_eu's own 92 steps x several params is
    the same shape as gefs_extended's ~154-file case this note names, so it
    keeps the 1h default rather than being tightened alongside the other two
    dense short-range models.
    """
    if not already_fetched(model_name, run_init):
        return True
    if (now - run_init) > timedelta(hours=FETCH_TOPUP_WINDOW_H):
        return False
    last = _last_fetch_attempt(model_name, run_init)
    if last is None:
        return True  # fetched before this top-up logic existed - give it one pass
    interval_h = get_model(model_name).get("fetch_retry_interval_h", FETCH_RETRY_INTERVAL_H)
    return (now - last) >= timedelta(hours=interval_h)


@dataclass
class FetchResult:
    model: str
    run_init: datetime
    steps: dict[str, tuple[int, float] | None]   # valid_time_iso -> (step, misalignment_h) | None
    files_written: list[Path] = field(default_factory=list)
    status: str = "ok"   # ok | not_yet_covering | error
    error: str | None = None

    def covering_steps(self) -> dict[str, int]:
        """Just the steps that are actually reachable, valid_time_iso -> step_hours."""
        return {vt: s[0] for vt, s in self.steps.items() if s is not None}


# Production's pipeline and its verification suite refer to this name. It is
# the same question have_usable_file() answers - "must this NOT be downloaded
# again?" - kept as an alias rather than a second implementation, so the
# reclaim tombstone and the corrupt-file check can never drift apart and
# disagree about the same file.
raw_file_present = have_usable_file
