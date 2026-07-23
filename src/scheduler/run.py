import logging
import os
import time
from datetime import UTC, datetime

import httpx

from src.config import load_models
from src.extract import registry as extract_registry
from src.extract.base import already_extracted, append_points, mark_extracted
from src.fetchers import registry as fetch_registry
from src.fetchers.base import already_fetched, cycle_run_inits, due_time, steps_for_run

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


def run_once() -> None:
    models = load_models()["models"]
    now = datetime.now(UTC)
    for model_name, model_config in models.items():
        if "cycles" not in model_config or "fetch" not in model_config:
            continue  # aggregator/reference entries (open_meteo, climatology): no direct fetch
        for run_init in cycle_run_inits(model_config["cycles"], now):
            steps = steps_for_run(model_config, run_init)
            if not any(v is not None for v in steps.values()):
                continue  # this run doesn't reach any eclipse-day archive valid time

            already_have_files = already_fetched(model_name, run_init)
            if not already_have_files:
                due = due_time(model_config.get("publication_lag_h", [0, 0]), run_init)
                if now < due:
                    continue
                try:
                    fetcher = fetch_registry.get_fetcher(model_config["fetch"])
                    result = fetcher(model_name, model_config, run_init)
                    log.info("fetched %s %s: %s", model_name, run_init.isoformat(), result.status)
                    already_have_files = bool(result.files_written)
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
    while True:
        try:
            run_once()
        except Exception:
            log.exception("run_once() failed")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
