"""T16: time-shift sim backfill via Open-Meteo's single-runs-api.

Populates points.parquet with real historical multi-model run history, using
whichever ECLIPSE_T is currently set (per CLAUDE.md's "Simulated-eclipse
testing": time-shift mode means ECLIPSE_T is a past 18:30 UTC, so every
fetch below targets that SAME date; only run_init varies) - this builds up
the T31 run-evolution "slider over run-init" view without waiting for T20's
native fetchers to accumulate history in real time.

Six models, matching config/models.yaml's models.open_meteo.cloud_provenance
table (T08) exactly - the points.parquet `model` label differs from Open-
Meteo's own `models=` id for two of them (icon_global/icon_eu) to avoid
silently colliding with this registry's own native icon_global/icon_eu
model names, which are a different pipeline/provenance entirely:

    label                       open_meteo_model_id            provenance
    gfs_global                  gfs_global                      native
    ecmwf_ifs025                ecmwf_ifs025                     derived
    om_icon_global               icon_global                     native
    om_icon_eu                   icon_eu                          native
    meteofrance_arpege_europe    meteofrance_arpege_europe        native
    ukmo_global                  ukmo_global_deterministic_10km   native (verify)

Usage:
    export ECLIPSE_T=2026-07-15T18:30:00Z   # pick any past 18:30 UTC
    uv run python scripts/backfill_open_meteo.py [--days N] [--cycles 0,6,12,18]

Idempotent - safe to re-run. Checks actual points.parquet contents for the
current target date before each fetch (see _existing_run_inits()'s docstring
for why this is NOT the same as the real scheduler's already_extracted()/
.extracted marker file - that marker is not target-date-aware, and a naive
reuse of it here was a real bug found running this for real, 2026-07-23).
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, date, datetime, timedelta

import polars as pl

from src.config import POINTS_PARQUET, eclipse_config
from src.extract.base import append_points, mark_extracted
from src.extract.open_meteo_extractor import extract
from src.fetchers.open_meteo_fetcher import fetch_single_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_open_meteo")

# CLAUDE.md fetch politeness: even though this script is single-threaded (no
# concurrency), a real full run (2026-07-23) hammering single-runs-api with
# zero delay between ~324 sequential requests produced 48 transient failures
# (connection resets/timeouts, none permanent - manual retries seconds later
# all succeeded). A small fixed pause between real HTTP calls (not before
# skips, those don't hit the network) cuts down how often that happens, on
# top of open_meteo_fetcher.py's own per-request retry/backoff.
REQUEST_PACING_S = 0.3

# label -> Open-Meteo's own `models=` id, per models.yaml's cloud_provenance table
MODELS = {
    "gfs_global": "gfs_global",
    "ecmwf_ifs025": "ecmwf_ifs025",
    "om_icon_global": "icon_global",
    "om_icon_eu": "icon_eu",
    "meteofrance_arpege_europe": "meteofrance_arpege_europe",
    "ukmo_global": "ukmo_global_deterministic_10km",
}


def _target_date() -> date:
    """ECLIPSE_T's own calendar date (UTC) - local reimplementation of the same
    computation open_meteo_extractor.py's _target_valid_times() does, kept
    import-light on purpose (see that module's docstring for why this file
    avoids pulling in src.fetchers.base for simple date math)."""
    cfg = eclipse_config()
    raw = os.environ.get("ECLIPSE_T") or cfg.get("t", "2026-08-12T18:30:00Z")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).date()


def _existing_run_inits(target_date: date) -> dict[str, set[datetime]]:
    """(model label -> set of run_inits) that already have real rows in
    points.parquet for exactly this target_date - the actual ground truth for
    "has this backfill_one() call already been done", used INSTEAD OF
    src.extract.base's already_extracted()/mark_extracted() marker file.

    BUG FOUND while running T16 for real (2026-07-23): that marker is a bare
    `data/raw/{model}/{run_init}/.extracted` file keyed only by (model,
    run_init) - it has no target-date awareness. ukmo_global's backfill label
    shares its raw-data directory with the live archiver's own primary
    Open-Meteo fetch path for the same model name, and several different
    ECLIPSE_T values have been used across this project's testing history
    (T31(c)'s 2026-07-25, this run's own date, etc.) - live-confirmed that 4
    ukmo_global run_inits already had a stale `.extracted` marker on disk from
    an EARLIER extraction against a DIFFERENT target date, which silently made
    the naive marker check skip real backfill work for the current date
    entirely (points.parquet had zero rows for those run_inits at the current
    target_date, despite `already_extracted()` reporting True). Checking
    points.parquet directly for a real row at this exact target_date is
    correct regardless of what happened for some other date previously.
    """
    if not POINTS_PARQUET.exists():
        return {}
    df = pl.read_parquet(POINTS_PARQUET)
    df = df.filter(pl.col("valid").dt.date() == target_date)
    out: dict[str, set[datetime]] = {}
    for model, run_init in df.select(["model", "run_init"]).unique().iter_rows():
        out.setdefault(model, set()).add(run_init)
    return out


def candidate_run_inits(days_back: int, cycles: list[int]) -> list[datetime]:
    now = datetime.now(UTC)
    out = []
    for d in range(days_back):
        day = (now - timedelta(days=d)).date()
        for h in cycles:
            candidate = datetime(day.year, day.month, day.day, h, tzinfo=UTC)
            if candidate <= now:
                out.append(candidate)
    return sorted(out)


def backfill_one(
    label: str, om_id: str, run_init: datetime, existing: dict[str, set[datetime]]
) -> tuple[str, str | None]:
    """Returns (outcome, error_detail_or_None)."""
    if run_init in existing.get(label, ()):
        return "already_extracted", None
    result = fetch_single_run(label, om_id, run_init)
    time.sleep(REQUEST_PACING_S)  # politeness pause after every real HTTP call
    if result.status != "ok":
        return result.status, result.error
    rows = extract(label, {}, run_init)  # model_config unused by this extractor
    append_points(rows)
    # Also set the shared .extracted marker (belt-and-suspenders, matches the
    # real scheduler's own convention) - NOT relied on for this script's own
    # idempotency check above, see _existing_run_inits()'s docstring for why.
    mark_extracted(label, run_init)
    existing.setdefault(label, set()).add(run_init)
    return f"ok:{len(rows)}rows", None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="how many days back to backfill")
    parser.add_argument(
        "--cycles", type=str, default="0,6,12,18", help="comma-separated cycle hours to try"
    )
    parser.add_argument(
        "--models", type=str, default=",".join(MODELS), help="comma-separated labels to backfill"
    )
    args = parser.parse_args()

    cycles = [int(h) for h in args.cycles.split(",")]
    labels = args.models.split(",")
    run_inits = candidate_run_inits(args.days, cycles)
    target_date = _target_date()
    existing = _existing_run_inits(target_date)
    log.info(
        "backfilling %d model(s) x %d candidate run_init(s) = up to %d fetches "
        "(target_date=%s, %d already present)",
        len(labels),
        len(run_inits),
        len(labels) * len(run_inits),
        target_date.isoformat(),
        sum(len(v) for v in existing.values()),
    )

    tally: dict[str, int] = {}
    for label in labels:
        om_id = MODELS[label]
        for run_init in run_inits:
            outcome, error_detail = backfill_one(label, om_id, run_init, existing)
            key = outcome.split(":")[0]
            tally[key] = tally.get(key, 0) + 1
            if error_detail:
                log.info(
                    "%-28s %s -> %s (%s)", label, run_init.isoformat(), outcome, error_detail
                )
            else:
                log.info("%-28s %s -> %s", label, run_init.isoformat(), outcome)

    log.info("done. outcome tally: %s", tally)


if __name__ == "__main__":
    main()
