"""Tool 2: for every gridded model, render every step of every already-
archived run_init (capped to the most recent few - renderings aren't final
yet, the full backfill across complete run history happens once the
rendering approach itself is settled, per explicit user direction) across
all 4 quantity fields, and write manifest.json describing what's available.

Unlike Tool 1 (every step of the LATEST run only) or Tool 3 (one step per
run, at a fixed valid time), Tool 2 needs every step of EVERY displayed
run - this is the expensive one (a model with 4 runs x ~100-200 steps x 4
fields is on the order of 2000-3000 renders per model). Ongoing/incremental
by nature, same as fetching: this only ever needs to render whatever is
newly archived since the last run, once each frame is written once - not a
recurring cost. Never delete previously rendered frames.

Fetching: none, same as generate_tool3_manifest.py - this only renders
from whatever is ALREADY archived.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.generate_tool2_manifest
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from src.config import DATA_RAW, get_model
from src.fetchers.base import full_range_steps
from src.viz.tool1_renderer import OUTPUT_DIR, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool2_manifest")

FIELDS = ["total", "low", "mid", "high"]
# Renderings aren't final yet (styling/polish still pending) - cap this
# initial real-data pass to the most recent few runs per model so it stays
# tractable. The real backfill across full run history happens once the
# rendering approach itself is settled, not now (same convention as
# generate_tool3_manifest.py's own MAX_RUNS_PER_MODEL).
MAX_RUNS_PER_MODEL = 4
MODELS = [
    ("gfs", "GFS"),
    ("gefs_extended", "GEFS Extended"),
    ("arome_france", "AROME France"),
    ("arpege_europe", "ARPEGE Europe"),
    ("ecmwf_hres", "ECMWF HRES"),
    ("ecmwf_ens", "ECMWF ENS"),
    ("aifs_single", "AIFS Single"),
    ("aifs_ens", "AIFS ENS"),
    ("icon_eu", "ICON EU"),
    ("icon_global", "ICON Global"),
]


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_run_init(dirname: str) -> datetime | None:
    try:
        return datetime.strptime(dirname, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _archived_run_inits(model_name: str) -> list[datetime]:
    d = DATA_RAW / model_name
    if not d.exists():
        return []
    run_inits = []
    for p in sorted(d.iterdir()):
        if not p.is_dir() or not any(p.iterdir()):
            continue  # skip stray/empty dirs (T35/T36 lesson - real test artifacts do turn up)
        run_init = _parse_run_init(p.name)
        if run_init is not None:
            run_inits.append(run_init)
    run_inits.sort()
    return run_inits[-MAX_RUNS_PER_MODEL:]


def _render_run(model_id: str, run_init: datetime, steps: list[int]) -> list[dict]:
    step_entries = []
    skipped = []
    for step in steps:
        images = {}
        has_data_by_field = {}
        any_real = False
        for field in FIELDS:
            path, has_data = render_frame(model_id, run_init, step, field)
            any_real = any_real or has_data
            has_data_by_field[field] = has_data
            images[field] = str(path.relative_to(OUTPUT_DIR)).replace("\\", "/")
        if not any_real:
            # Nothing was actually published for this step in ANY field -
            # same "exclude, don't list a permanent (no data) tick" logic
            # as generate_tool1_manifest.py.
            skipped.append(step)
            continue
        step_entries.append({
            "h": step,
            "valid": _iso_z(run_init + timedelta(hours=step)),
            "images": images,
            "has_data": has_data_by_field,
        })
    if skipped:
        log.info(
            "%s %s: excluded %d step(s) with no real data in any field",
            model_id, run_init.isoformat(), len(skipped),
        )
    return step_entries


def main() -> None:
    manifest_models = []
    for model_id, label in MODELS:
        model_config = get_model(model_id)
        run_inits = _archived_run_inits(model_id)
        log.info("%s: %d archived run(s) to render (capped to %d)", model_id, len(run_inits), MAX_RUNS_PER_MODEL)

        run_entries = []
        for run_init in run_inits:
            steps = full_range_steps(model_config, run_init)
            log.info(
                "%s %s: %d steps x %d fields",
                model_id, run_init.isoformat(), len(steps), len(FIELDS),
            )
            step_entries = _render_run(model_id, run_init, steps)
            run_entries.append({
                "run_init": _iso_z(run_init),
                "steps": step_entries,
            })
            log.info("%s %s: rendered %d steps", model_id, run_init.isoformat(), len(step_entries))

        manifest_models.append({
            "id": model_id,
            "label": label,
            "runs": run_entries,
        })

    manifest = {"generated_at": _iso_z(datetime.now(UTC)), "models": manifest_models}
    manifest_path = OUTPUT_DIR / "tool2_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s", manifest_path)


if __name__ == "__main__":
    main()
