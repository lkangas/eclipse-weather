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

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]
REQUEST_TIMEOUT_S = 30.0


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


def fetch_single_run(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """STUB for T16 (time-shift sim backfill). NOT implemented here.

    Would call single-runs-api.open-meteo.com (models.yaml's
    models.open_meteo.api.single_runs) with a `run=<ISO8601 no seconds>`
    query param (e.g. `run=2026-07-20T00:00`) once per historical run_init,
    looping over every cycle in the desired backfill window, to reconstruct
    true native per-run cloud_cover_low/mid/high history.

    T08 confirmed this host DOES carry real L/M/H per run (unlike
    previous_runs above); retention is ~April 2026 onward (~3.5 months as of
    2026-07-22 — see models.yaml's single_runs.retention), which comfortably
    covers a 16-day ECLIPSE_T time-shift window but not deep historical
    backtests. This is the real backfill path for T16 — not built in this
    pass; the caller would need to enumerate historical run_inits (e.g. via
    `base.cycle_run_inits` over a wider lookback) and call this endpoint once
    per run.
    """
    raise NotImplementedError(
        "T16 backfill via single-runs-api.open-meteo.com not implemented in this pass. "
        "See models.yaml models.open_meteo.api.single_runs (param: run=<ISO8601 no "
        "seconds>). T08 confirmed this host carries true native per-run "
        "cloud_cover_low/mid/high, retained from ~April 2026 onward. Loop over historical "
        "run_inits (e.g. base.cycle_run_inits with a wide lookback) and call this endpoint "
        "once per run to build the T16 time-shift backfill dataset."
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
