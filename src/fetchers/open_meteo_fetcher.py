"""Open-Meteo JSON fetcher.

Registered fetch key: ``open_meteo_json`` (see ``config/models.yaml``'s
``models.ukmo_global.fetch``).

This module's primary target this pass is ``ukmo_global`` — Open-Meteo's
``/v1/forecast`` endpoint is its confirmed PRIMARY data path (not just a
backfill convenience), per ``models.ukmo_global.source.primary`` and T06.

Live-forecast mode vs true run_init
------------------------------------
Open-Meteo's live ``/v1/forecast`` endpoint always serves the "current best"
forecast for a model — there is no ``?run=`` parameter on this host, so there
is no true historical run_init to key off (that only exists on the
single-runs host — see the T16 stubs at the bottom of this file). We
approximate: round "now" down to the most recent cycle hour for the model
(via ``base.cycle_run_inits``, keyed off ``model_config["cycles"]`` —
00/06/12/18 for ukmo_global) and use that as the run_init LABEL for
``raw_output_dir(model_name, run_init)``.

This is a label of convenience, not a guarantee that Open-Meteo's answer at
call time actually reflects data from that exact model cycle: Open-Meteo
does not expose which underlying run backs its "live" answer, and its own
documented extra delay (~4 h on top of the model's own publication lag — see
``models.ukmo_global.publication_lag_h``'s T06 note) means the true
underlying run is quite plausibly one cycle further back than the naive
"most recent past cycle boundary" computed here, especially when called
shortly after a cycle boundary ticks over. Good enough for the archiver's
"has something landed for roughly this cycle" purpose; T21/extract should
not treat this label as an exact provenance guarantee, only as an
approximate bucket.

Provenance note (Hard Constraint #3)
-------------------------------------
``models.ukmo_global.cloud.levels.provenance_via_open_meteo`` is currently
``verify``, not ``confirmed`` — Open-Meteo's docs make no explicit
native-vs-derived statement for this model (unlike gfs/ecmwf/icon/arpege).
This fetcher only writes the raw JSON response to disk; it does not assign a
provenance tag (that happens at T21/extract time, reading the flag from
models.yaml). Do not treat the mere presence of non-null values here as
proof of "native" — see the note in models.yaml for what would actually
resolve it.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import httpx

from src.config import eclipse_config, load_sites
from src.fetchers.base import (
    FetchResult,
    cycle_run_inits,
    raw_output_dir,
    steps_for_run,
    target_valid_times,
)
from src.fetchers.registry import register

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]
REQUEST_TIMEOUT_S = 30.0

# CLAUDE.md fetch politeness: exponential backoff, honor Open-Meteo's
# non-commercial rate limits. Live-confirmed necessary during T16's real
# full-scale run (2026-07-23): a sustained sequential run of ~324 single-runs-
# api calls with zero delay between them produced 48 transient failures
# (connection resets/timeouts - manually retrying the exact same calls
# seconds later succeeded every time, so these were not permanent "this run
# isn't available" 400s), none of which were literal HTTP 429s but all of the
# same "back off and retry" character. get_with_retry() below is shared by
# fetch_single_run() for this reason.
MAX_RETRIES = 4
RETRY_BACKOFF_BASE_S = 1.5


def _get_with_retry(url: str, params: dict) -> httpx.Response:
    """httpx.get with exponential backoff on transient failures (connection
    errors/timeouts, and HTTP 429) - see MAX_RETRIES's docstring above for why
    this exists. Non-retryable responses (e.g. a real HTTP 400/200) are
    returned as-is on the first attempt; callers still do their own status
    handling on the returned Response."""
    last_exc: httpx.HTTPError | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                wait_s = RETRY_BACKOFF_BASE_S * (2**attempt)
                logger.warning(
                    "single-runs-api request failed (%s), retrying in %.1fs (attempt %d/%d)",
                    exc,
                    wait_s,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait_s)
                continue
            raise
        if resp.status_code == 429 and attempt < MAX_RETRIES - 1:
            wait_s = RETRY_BACKOFF_BASE_S * (2**attempt)
            logger.warning(
                "single-runs-api HTTP 429, retrying in %.1fs (attempt %d/%d)",
                wait_s,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(wait_s)
            continue
        return resp
    if last_exc is not None:
        raise last_exc
    return resp  # last 429 response after exhausting retries - let the caller handle it


def _site_coords() -> tuple[list[float], list[float]]:
    """Lat/lon lists straight from config/sites.yaml's candidate site list —
    per the task brief, use the sites already defined there rather than
    inventing arbitrary grid points."""
    sites = load_sites()["sites"]
    lats = [s["lat"] for s in sites]
    lons = [s["lon"] for s in sites]
    return lats, lons


def _approx_run_init(model_config: dict, now: datetime) -> datetime:
    """'now' rounded down to the most recent cycle-hour boundary for this
    model — used only as a directory-naming label (see module docstring)."""
    run_inits = cycle_run_inits(model_config["cycles"], now, lookback_hours=48)
    if not run_inits:
        # Extremely defensive fallback — cycle_run_inits with a 48h lookback
        # should always yield at least one candidate for any real cycle table.
        return now.replace(minute=0, second=0, microsecond=0)
    return run_inits[-1]


def _valid_date_range() -> tuple[str, str]:
    """ISO calendar-date bounds (UTC) covering every eclipse-day archive
    valid time, for Open-Meteo's start_date/end_date params."""
    valid_times = target_valid_times(eclipse_config()["archive_valid_hours_utc"])
    start = min(vt.date() for vt in valid_times)
    end = max(vt.date() for vt in valid_times)
    return start.isoformat(), end.isoformat()


def _model_id(model_config: dict) -> str:
    try:
        return model_config["source"]["primary"]["model_id"]
    except (KeyError, TypeError) as exc:
        raise KeyError(
            "open_meteo_json fetcher expects model_config['source']['primary']['model_id'] "
            "(the models.yaml shape used by ukmo_global) — got a different shape."
        ) from exc


@register("open_meteo_json")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch current Open-Meteo hourly cloud_cover[/_low/_mid/_high] for every
    site in config/sites.yaml, for the model_id configured under
    model_config["source"]["primary"]["model_id"], and save the raw JSON
    response body under raw_output_dir(model_name, run_init).

    `run_init` here is the approximate cycle-boundary LABEL described in the
    module docstring, not a byte-range-subsetting run identifier — callers
    that just want "now" can pass `_approx_run_init(model_config, datetime.now(UTC))`,
    which is exactly what `__main__` below does.
    """
    model_id = _model_id(model_config)
    lats, lons = _site_coords()
    start_date, end_date = _valid_date_range()

    steps = steps_for_run(model_config, run_init)

    params = {
        "latitude": ",".join(str(v) for v in lats),
        "longitude": ",".join(str(v) for v in lons),
        "hourly": ",".join(HOURLY_VARS),
        "models": model_id,
        "timezone": "UTC",
        "start_date": start_date,
        "end_date": end_date,
    }

    try:
        resp = httpx.get(FORECAST_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error", error=str(exc)
        )

    if resp.status_code != 200:
        # Open-Meteo returns HTTP 400 + {"error": true, "reason": "..."} for a
        # date outside its currently-served forecast horizon — that's the
        # expected "run doesn't cover T yet" case, not a hard failure, for
        # dates near/at the real eclipse day before the model's horizon
        # reaches it. Distinguish that from a genuine error.
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        reason = body.get("reason", "") if isinstance(body, dict) else ""
        if resp.status_code == 400 and "out of allowed range" in reason:
            return FetchResult(
                model=model_name,
                run_init=run_init,
                steps=steps,
                status="not_yet_covering",
                error=reason,
            )
        return FetchResult(
            model=model_name,
            run_init=run_init,
            steps=steps,
            status="error",
            error=f"HTTP {resp.status_code}: {reason or resp.text[:200]}",
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error", error=str(exc)
        )

    if isinstance(payload, dict) and payload.get("error"):
        return FetchResult(
            model=model_name,
            run_init=run_init,
            steps=steps,
            status="error",
            error=payload.get("reason", "Open-Meteo returned an error object"),
        )
    if not isinstance(payload, list) or not payload:
        shape_error = f"Expected a non-empty list from Open-Meteo, got: {type(payload)}"
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error", error=shape_error
        )

    out_dir = raw_output_dir(model_name, run_init)
    out_path = out_dir / "forecast.json"
    out_path.write_text(resp.text, encoding="utf-8")

    return FetchResult(
        model=model_name,
        run_init=run_init,
        steps=steps,
        files_written=[out_path],
        status="ok",
    )


# ---------------------------------------------------------------------------
# T16 backfill extension points — NOT implemented in this pass.
#
# These are separate hosts from the live /v1/forecast endpoint used by
# fetch() above; see config/models.yaml's models.open_meteo.api block.
# Deliberately left as stubs per this task's scope — full backfill logic is
# T16's job, once it's actually being built.
# ---------------------------------------------------------------------------


def fetch_previous_runs(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """STUB for T16 (time-shift sim backfill). NOT implemented here.

    Would call previous-runs-api.open-meteo.com (models.yaml's
    models.open_meteo.api.previous_runs) using its `_previous_dayN`
    lead-time-offset suffix mechanism (N=0..7) to reconstruct several
    historical runs' forecasts for a fixed valid time from a single request.

    T08 CAVEAT (already recorded in models.yaml): this mechanism returns
    HTTP 200 with every value NULL for cloud_cover_low/mid/high_previous_dayN,
    for every model and offset tested — it only works for the unsplit total
    `cloud_cover`. Do NOT build true L/M/H run-evolution backfill against this
    host; see fetch_single_run below for the host that actually carries L/M/H
    history. This function would still be useful for a total-cloud-only
    run-evolution backfill if that's ever wanted on its own.
    """
    raise NotImplementedError(
        "T16 backfill via previous-runs-api.open-meteo.com not implemented in this pass. "
        "See models.yaml models.open_meteo.api.previous_runs and its T08 caveat: "
        "cloud_cover_low/mid/high are NULL via _previous_dayN suffixes on this host "
        "(only unsplit cloud_cover works there) — use fetch_single_run for true L/M/H "
        "per-run history instead."
    )


SINGLE_RUN_URL = "https://single-runs-api.open-meteo.com/v1/forecast"


def fetch_single_run(model_label: str, open_meteo_model_id: str, run_init: datetime) -> FetchResult:
    """T16 backfill: one historical run via single-runs-api.open-meteo.com
    (`run=<ISO8601 no seconds>`), for whichever eclipse-day archive valid
    times ECLIPSE_T currently resolves to (T16's sim mode works by
    overriding ECLIPSE_T to a past date - see CLAUDE.md's "Simulated-eclipse
    testing" section - so this fetches the SAME target date every call, only
    `run_init` changes, building up the run-evolution slider one historical
    run at a time).

    Unlike fetch() above, this is NOT registered under @register() and does
    NOT take (model_name, model_config) tied to this project's own models.yaml
    registry - single-runs-api backfills models Open-Meteo aggregates that
    mostly don't correspond 1:1 to this project's own native fetchers (see
    models.yaml's models.open_meteo.cloud_provenance table for the six
    candidates and scripts/backfill_open_meteo.py for the orchestration and
    model-label scheme, incl. why two labels are "om_"-prefixed to avoid
    colliding with this registry's own icon_global/icon_eu model names).

    model_label: the points.parquet `model` value to write (e.g. "gfs_global",
        "om_icon_eu", "ukmo_global" - see backfill_open_meteo.py's mapping).
    open_meteo_model_id: the exact `models=` value Open-Meteo expects
        (e.g. "gfs_global", "dwd_icon_eu" - NOT always the same string as
        model_label; confirm per-id via T08's findings before adding a new one).

    BUG FOUND + FIXED while running T16 for real (2026-07-23): single-runs-api
    is NOT the same param surface as the live /v1/forecast host `fetch()` uses
    above - passing `start_date`/`end_date` (as this function originally did,
    copy-pasted from `fetch()`) gets a hard HTTP 400 every time: `{"reason":
    "Parameter 'start_date' must not be set","error":true}`, live-confirmed
    against the real endpoint, not a docs guess. The real mechanism is
    `forecast_days` (an integer day-count *from `run` forward*, not a
    calendar-date window) - so this now computes just enough days to reach
    ECLIPSE_T's own date, +1 day margin (confirmed necessary: a 06Z run only
    reaches 05:00 on day run+N with forecast_days=N, one hour short of an
    18:30Z target - live-tested). Also confirmed live: requesting more
    forecast_days than a run's real forecast horizon is NOT an error - it's
    HTTP 200 with silently-null values past the real horizon - so this
    function now also checks the actual returned values at the wanted valid
    times and downgrades to status="not_yet_covering" if they're all null,
    instead of reporting "ok" for a file that extract() would turn into
    all-None PointRows.
    """
    lats, lons = _site_coords()
    valid_times = target_valid_times(eclipse_config()["archive_valid_hours_utc"])
    target_date = max(vt.date() for vt in valid_times)
    forecast_days = max((target_date - run_init.date()).days + 1, 1)

    params = {
        "latitude": ",".join(str(v) for v in lats),
        "longitude": ",".join(str(v) for v in lons),
        "hourly": ",".join(HOURLY_VARS),
        "models": open_meteo_model_id,
        "run": run_init.strftime("%Y-%m-%dT%H:%M"),
        "timezone": "UTC",
        "forecast_days": forecast_days,
    }
    # No steps_for_run() here (deliberately) - single-runs-api is forecast-days-
    # from-run-init based, not forecast-hour-offset based, and these backfill
    # labels have no models.yaml cycles/steps entry to compute against. steps
    # stays empty; FetchResult.status/files_written are what callers should check.
    steps: dict = {}

    try:
        resp = _get_with_retry(SINGLE_RUN_URL, params)
    except httpx.HTTPError as exc:
        return FetchResult(
            model=model_label, run_init=run_init, steps=steps, status="error", error=str(exc)
        )

    if resp.status_code != 200:
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        reason = body.get("reason", "") if isinstance(body, dict) else ""
        # Seen for: run predates this model's single-runs retention window,
        # OR run_init's own forecast horizon doesn't reach start_date/end_date.
        # Both are the expected "this run can't inform this target date" case.
        if resp.status_code == 400:
            return FetchResult(
                model=model_label,
                run_init=run_init,
                steps=steps,
                status="not_yet_covering",
                error=reason or f"HTTP 400: {resp.text[:200]}",
            )
        return FetchResult(
            model=model_label,
            run_init=run_init,
            steps=steps,
            status="error",
            error=f"HTTP {resp.status_code}: {reason or resp.text[:200]}",
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        return FetchResult(
            model=model_label, run_init=run_init, steps=steps, status="error", error=str(exc)
        )

    if isinstance(payload, dict) and payload.get("error"):
        return FetchResult(
            model=model_label,
            run_init=run_init,
            steps=steps,
            status="error",
            error=payload.get("reason", "Open-Meteo returned an error object"),
        )
    if not isinstance(payload, list) or not payload:
        return FetchResult(
            model=model_label,
            run_init=run_init,
            steps=steps,
            status="error",
            error=f"Expected a non-empty list from Open-Meteo, got: {type(payload)}",
        )

    # HTTP 200 does not mean this run actually reaches ECLIPSE_T - single-runs-api
    # silently returns null for hours past the run's real forecast horizon rather
    # than erroring (live-confirmed, see docstring). Check the wanted valid times
    # actually have a real value at at least one site before calling this "ok".
    wanted_keys = {vt.strftime("%Y-%m-%dT%H:%M") for vt in valid_times}
    has_real_data = False
    for site_payload in payload:
        hourly = site_payload.get("hourly", {}) if isinstance(site_payload, dict) else {}
        times = hourly.get("time", [])
        cloud_total = hourly.get("cloud_cover", [])
        for idx, t in enumerate(times):
            if t in wanted_keys and idx < len(cloud_total) and cloud_total[idx] is not None:
                has_real_data = True
                break
        if has_real_data:
            break
    if not has_real_data:
        return FetchResult(
            model=model_label,
            run_init=run_init,
            steps=steps,
            status="not_yet_covering",
            error=(
                f"run_init {run_init.isoformat()} returned HTTP 200 but every wanted "
                f"valid time is null (its real forecast horizon likely doesn't reach "
                f"{target_date.isoformat()})"
            ),
        )

    out_dir = raw_output_dir(model_label, run_init)
    out_path = out_dir / "forecast.json"
    out_path.write_text(resp.text, encoding="utf-8")

    return FetchResult(
        model=model_label, run_init=run_init, steps=steps, files_written=[out_path], status="ok"
    )


if __name__ == "__main__":
    # Manual smoke test — run with:
    #   export ECLIPSE_T=2026-07-25T18:30:00Z   (bash)
    #   uv run python -m src.fetchers.open_meteo_fetcher
    from src.config import get_model

    _model_name = "ukmo_global"
    _model_config = get_model(_model_name)
    _now = datetime.now(UTC)
    _run_init = _approx_run_init(_model_config, _now)

    print(f"model={_model_name} approx_run_init={_run_init.isoformat()} now={_now.isoformat()}")
    result = fetch(_model_name, _model_config, _run_init)
    print(f"status={result.status} error={result.error}")
    print(f"files_written={[str(p) for p in result.files_written]}")
    print(f"covering_steps={result.covering_steps()}")
