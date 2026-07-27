"""Fetch newly-added fields for runs already in the archive.

Adding a field to a model does NOT make it appear on past runs: their raw was
downloaded before the field existed, so the frames can never be rendered from
what is on disk. Every already-rendered run therefore reads as incomplete the
moment supported_fields() starts advertising the new field - 71 of them when
gfs/gefs gained temp and rain.

This fetches ONLY the missing units for runs still inside the top-up window,
after which nothing is incomplete. It does not render: the render workers
discover work by comparing a run's raw-file count against .last_render, so new
raw landing in a run directory makes that run look grown and they re-render it
on their own.

Cheapest models first, deliberately. The two big ensembles cost ~62% of the
total because ECMWF open-data ships all 51 members for a scalar field we only
take the mean of, so ordering by cost means the useful bulk is done early and
the expensive tail can be killed without losing it.

    python -m scripts.backfill_fields --hours 48            # what it would do
    python -m scripts.backfill_fields --hours 48 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from src.config import DATA_RAW, get_model, load_models
from src.fetchers import registry as fetch_registry
from src.fetchers.base import format_init_dir, full_range_steps
from src.viz.frame_renderer import supported_fields

log = logging.getLogger("backfill_fields")

# Cheapest first - see the module note. Measured MB/step for temperature.
_COST_ORDER = [
    "gfs", "gefs_extended", "aifs_single", "ecmwf_hres",
    "icon_eu", "icon_global", "arome_france", "arpege_europe",
    "ecmwf_ens", "aifs_ens",
]

# No filename-to-field table here, deliberately. The first version had one and
# it was wrong within minutes: the four fetcher families name their output
# differently - f006_temp.grib2, temp_f006.grib2, ..._T_2M.grib2, ..._SP1_... -
# so matching on "temp" silently found nothing for ICON and Meteo-France and
# reported those models as already complete.
#
# Instead just re-invoke the fetcher for every run in the window. Every fetcher
# is idempotent per FILE (have_usable_file), so it stats what exists, downloads
# only what is absent, and that is exactly the new field's files. No knowledge
# of naming required, and it cannot drift when a fetcher changes its layout.


def _archived_runs(model_id: str, since: datetime) -> list[datetime]:
    d = DATA_RAW / model_id
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if not (p.is_dir() and len(p.name) == 10 and p.name.isdigit()):
            continue
        try:
            ri = datetime.strptime(p.name, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if ri >= since and any(f.is_file() and not f.name.startswith(".") for f in p.iterdir()):
            out.append(ri)
    return sorted(out, reverse=True)   # newest first, same as the pipeline


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=48.0,
                    help="how far back to backfill (default 48, the top-up window)")
    ap.add_argument("--apply", action="store_true", help="actually fetch")
    ap.add_argument("--model", action="append", dest="models")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    now = datetime.now(UTC)
    since = now - timedelta(hours=args.hours)
    all_models = load_models()["models"]
    order = [m for m in _COST_ORDER if m in all_models] + [
        m for m in all_models if m not in _COST_ORDER]

    planned, fetched, failed, t0 = 0, 0, 0, time.time()
    for model_id in order:
        if args.models and model_id not in args.models:
            continue
        cfg = all_models[model_id]
        if "fetch" not in cfg or "cycles" not in cfg:
            continue
        try:
            fetcher = fetch_registry.get_fetcher(cfg["fetch"])
        except Exception:
            log.warning("%s: no fetcher registered, skipping", model_id)
            continue

        new_fields = [f for f in supported_fields(model_id) if f in ("temp", "rain")]
        if not new_fields:
            continue
        for run_init in _archived_runs(model_id, since):
            run_dir = DATA_RAW / model_id / format_init_dir(run_init)
            before = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
            steps = full_range_steps(get_model(model_id), run_init)
            planned += 1
            log.info("%s %s: re-offering %d steps for %s",
                     model_id, run_init.isoformat(), len(steps), ",".join(new_fields))
            if not args.apply:
                continue
            try:
                # The fetcher is idempotent per file, so this re-offers every
                # step and downloads only what is absent - which is exactly the
                # new field's files.
                result = fetcher(model_id, cfg, run_init)
                if result.status == "error":
                    failed += 1
                    log.error("%s %s: %s", model_id, run_init.isoformat(), result.error)
                else:
                    after = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
                    added = (after - before) / 1e6
                    fetched += 1
                    log.info("%s %s: +%.1f MB", model_id, run_init.isoformat(), added)
            except Exception:
                failed += 1
                log.exception("%s %s: fetch raised", model_id, run_init.isoformat())

    el = time.time() - t0
    log.info("%s: %d run(s) needed fields, %d fetched, %d failed, %.1f min",
             "APPLIED" if args.apply else "DRY RUN", planned, fetched, failed, el / 60)
    if not args.apply:
        log.info("nothing was fetched - rerun with --apply")


if __name__ == "__main__":
    sys.exit(main())
