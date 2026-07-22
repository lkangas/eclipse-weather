import logging
import os
import time
from datetime import UTC, datetime

import httpx

from src.config import load_models
from src.fetchers import registry
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
            if already_fetched(model_name, run_init):
                continue
            due = due_time(model_config.get("publication_lag_h", [0, 0]), run_init)
            if now < due:
                continue
            steps = steps_for_run(model_config, run_init)
            if not any(v is not None for v in steps.values()):
                continue  # this run doesn't reach any eclipse-day archive valid time
            try:
                fetcher = registry.get_fetcher(model_config["fetch"])
                result = fetcher(model_name, model_config, run_init)
                log.info("fetched %s %s: %s", model_name, run_init.isoformat(), result.status)
            except Exception as e:
                log.error("fetch failed for %s %s: %s", model_name, run_init.isoformat(), e)
    ping_healthcheck()


def main() -> None:
    # Import fetcher submodules here (not at module load) purely for their
    # @register(...) side-effects, once T20's modules exist.
    from src import fetchers  # noqa: F401

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
