"""Render a chosen SUBSET of fields for archived runs.

Companion to scripts/backfill_fields.py: that fetches raw for a newly-added
field, this draws it. The scheduler's own render workers would normally do the
drawing on their own - they notice a run whose raw-file count has grown and
re-render it - but they are long-running Python processes that imported
frame_renderer at startup, so a worker started before a field existed keeps the
OLD supported_fields() in memory and stat-skips the new field forever. Observed
directly: 209 steps "rendered" in 0.4 s with zero temp/rain frames produced.
Restarting the workers fixes that; this script exists for when restarting them
is not on the table (mid-render on the archive of record, torn-frame risk).

Running CONCURRENTLY with those workers is safe here, and only because the
field sets are disjoint: stale workers touch cloud fields only, this touches
temp/rain only, so no two processes ever write the same PNG path. Point it at
a field a live worker also renders and that guarantee is gone.

Deliberately does NOT write the .last_render marker. That is the workers'
bookkeeping - the raw-count-vs-marker comparison is how they detect a grown
run - and stamping it here would tell them a run is fully drawn when only some
of its fields are.

    python -m scripts.render_fields --hours 48 --field temp --field rain
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

from src.config import DATA_RAW
from src.viz.frame_renderer import _MODEL_READERS, render_frame, supported_fields

log = logging.getLogger("render_fields")


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
        if ri >= since:
            out.append(ri)
    return sorted(out, reverse=True)   # newest first, as everywhere else


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--field", action="append", dest="fields", required=True)
    ap.add_argument("--model", action="append", dest="models")
    ap.add_argument("--passes", type=int, default=1,
                    help="repeat N times - raw still arriving means one pass "
                         "cannot draw everything; existing frames are skipped "
                         "cheaply, so a later pass only costs the new steps")
    ap.add_argument("--pass-gap-s", type=float, default=300.0)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    for pass_no in range(1, args.passes + 1):
        now = datetime.now(UTC)
        since = now - timedelta(hours=args.hours)
        drawn = empty = failed = 0
        t0 = time.time()

        # _MODEL_READERS, not a hardcoded list - the reader registry is what
        # actually decides renderability (CLAUDE.md constraint #2's spirit).
        for model_id in sorted(_MODEL_READERS):
            if args.models and model_id not in args.models:
                continue
            # A field this model structurally cannot produce is not an error to
            # report per run - GFS is the only rain-capable model, so asking
            # for rain everywhere would otherwise log 15 models' worth of noise.
            fields = [f for f in args.fields if f in supported_fields(model_id)]
            if not fields:
                continue
            for run_init in _archived_runs(model_id, since):
                for field in fields:
                    n_d = n_e = 0
                    for step in _steps_on_disk(model_id, run_init):
                        try:
                            _, has_data = render_frame(model_id, run_init, step, field)
                            if has_data:
                                n_d += 1
                            else:
                                n_e += 1
                        except Exception:
                            failed += 1
                            log.exception("%s %s +%sh %s", model_id, run_init, step, field)
                    drawn += n_d
                    empty += n_e
                    if n_d or n_e:
                        log.info("%s %s %s: %d frame(s), %d without data",
                                 model_id, run_init.isoformat(), field, n_d, n_e)

        log.info("pass %d/%d: %d frame(s) drawn, %d without data, %d failed, %.1f min",
                 pass_no, args.passes, drawn, empty, failed, (time.time() - t0) / 60)
        if pass_no < args.passes:
            time.sleep(args.pass_gap_s)


def _steps_on_disk(model_id: str, run_init: datetime) -> list[int]:
    """Which steps this run could be drawn for, taken from models.yaml rather
    than from filenames - the four fetcher families name their output four
    different ways (f006_temp.grib2, temp_f006.grib2, ..._T_2M.grib2,
    ..._SP1_...), and an earlier filename-derived version silently found no
    steps at all for ICON and Meteo-France."""
    from src.config import get_model
    from src.fetchers.base import full_range_steps
    return full_range_steps(get_model(model_id), run_init)


if __name__ == "__main__":
    sys.exit(main())
