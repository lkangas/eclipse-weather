import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.config import DATA_RAW, eclipse_config

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
    available_steps: list[int], target_offset_hours: float
) -> tuple[int, float] | None:
    """Nearest available forecast-hour step to a target offset. Returns
    (step, |misalignment_hours|), or None if the target is before init or
    beyond the model's max reach (run doesn't cover it yet)."""
    if target_offset_hours < 0:
        return None
    if target_offset_hours > max(available_steps):
        return None
    step = min(available_steps, key=lambda s: abs(s - target_offset_hours))
    return step, abs(step - target_offset_hours)


def _available_steps_for_cycle(model_config: dict, run_init: datetime) -> list[int]:
    """Every published forecast-hour step for this specific run_init's cycle,
    capped by that cycle's own max reach. `cycles:` gives a max forecast
    length PER CYCLE HOUR (e.g. gefs_extended's 00Z reaches 840h but
    06/12/18Z only reach 384h; ecmwf_hres and ukmo_global have similar
    splits) - this must additionally cap `steps:`'s shared cadence spec, or
    a short cycle gets asked for steps its run was never going to publish.
    """
    available = generate_available_steps(model_config["steps"])
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
    """Cheap idempotency check: has anything been written for this run already?"""
    d = DATA_RAW / model_name / format_init_dir(run_init)
    return d.exists() and any(d.iterdir())


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
    """
    if not already_fetched(model_name, run_init):
        return True
    if (now - run_init) > timedelta(hours=FETCH_TOPUP_WINDOW_H):
        return False
    last = _last_fetch_attempt(model_name, run_init)
    if last is None:
        return True  # fetched before this top-up logic existed - give it one pass
    return (now - last) >= timedelta(hours=FETCH_RETRY_INTERVAL_H)


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
