"""Continuously collects new full-range runs for every model wired into
Tool 1 (data/raw_latest/), as new runs become available upstream.

Deliberately a SEPARATE loop from the eclipse archiver's own scheduler
(src/scheduler/run.py) - that one is proven, reliable, and handles data/raw/'s
eclipse-cropped fetches on its own independent cadence; this script must
never risk destabilizing it. Same reasoning as DATA_RAW_LATEST being a
separate tree from DATA_RAW in the first place (see src/config.py).

Desktop-only intentionally: raw archive data is kept on this dev desktop
(disk isn't constrained here, unlike petzval) until the rendering approach
is finalized - only rendered output moves to petzval eventually, and only
once that's decided. This script is not part of the production Dockerfile/
docker-compose.yml and should never be added there without a deliberate
decision to do so.

Fetch-only, no rendering - re-rendering/regenerating manifest.json stays a
separate, manual, on-demand step (scripts/generate_tool1_manifest.py) while
the rendering approach is still being iterated.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.collect_full_range
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from src.config import get_model
from src.fetchers.base import latest_available_run_init
from src.fetchers.dwd_bz2_fetcher import fetch_full_range as _fetch_full_range_http_bz2
from src.fetchers.ecmwf_opendata_fetcher import fetch_full_range as _fetch_full_range_ecmwf
from src.fetchers.herbie_fetcher import fetch_full_range as _fetch_full_range_herbie
from src.fetchers.meteofrance_fetcher import fetch_full_range as _fetch_full_range_http_grib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collect_full_range")

CHECK_INTERVAL_S = 900  # 15 min - full-range fetches are heavy, no need for the
                        # main archiver's 5-min cadence; most models only
                        # publish a new run every 6-12h anyway.

MODELS = [
    "gfs", "gefs_extended", "arome_france", "arpege_europe",
    "ecmwf_hres", "ecmwf_ens", "aifs_single", "aifs_ens",
    "icon_eu", "icon_global",
]

# models.yaml `fetch:` value -> this model's fetch_full_range() entry point,
# same dispatch table as scripts/generate_tool1_manifest.py.
_FETCH_FULL_RANGE_BY_KEY = {
    "herbie": _fetch_full_range_herbie,
    "http_grib": _fetch_full_range_http_grib,
    "ecmwf-opendata": _fetch_full_range_ecmwf,
    "http_bz2": _fetch_full_range_http_bz2,
}


def run_once() -> None:
    now = datetime.now(UTC)
    for model_id in MODELS:
        model_config = get_model(model_id)
        run_init = latest_available_run_init(model_config, now)
        if run_init is None:
            log.info("%s: no due run_init yet", model_id)
            continue

        fetch_fn = _FETCH_FULL_RANGE_BY_KEY.get(model_config.get("fetch"))
        if fetch_fn is None:
            log.warning("%s: no fetch_full_range() dispatch for fetch key %r",
                        model_id, model_config.get("fetch"))
            continue

        try:
            result = fetch_fn(model_id, model_config, run_init)
        except Exception:
            log.exception("%s: fetch_full_range() raised for run_init=%s",
                          model_id, run_init.isoformat())
            continue

        log.info(
            "%s: run_init=%s status=%s files_written=%d%s",
            model_id, run_init.isoformat(), result.status, len(result.files_written),
            f" error={result.error}" if result.error else "",
        )


def main() -> None:
    log.info("collect_full_range: starting, checking every %ds", CHECK_INTERVAL_S)
    while True:
        try:
            run_once()
        except Exception:
            log.exception("collect_full_range: run_once() failed, will retry next interval")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
