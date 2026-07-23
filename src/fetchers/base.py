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


def full_range_steps(model_config: dict, run_init: datetime) -> list[int]:
    """Every available step for this run, uncropped - Tool 1's general-
    purpose "latest run of every model" explorer wants the whole forecast
    horizon a run actually publishes, not just the eclipse-day archive
    hours steps_for_run() targets."""
    return _available_steps_for_cycle(model_config, run_init)


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
