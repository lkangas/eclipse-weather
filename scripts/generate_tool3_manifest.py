"""Tool 3: for every gridded model, render one frame per already-archived
run_init at the step nearest eclipse_t() (see src/fetchers/base.py -
ECLIPSE_T env var, never hardcoded), across all 4 quantity fields, and
write manifest.json describing what's available.

Unlike Tool 1 (every step of the LATEST run) or Tool 2 (every step of
EVERY run), Tool 3 only ever needs ONE step per run - the whole point is
comparing models/runs at a single fixed valid time - so this is a cheap
manifest to (re)generate even across every archived run of every model.

Fetching: none. Unlike generate_tool1_manifest.py, this script only
renders from whatever is ALREADY archived - Tool 3's run-init axis is a
historical view, not a "get the latest" tool, so there is no fetch step
here at all.

Usage (inside Docker, GRIB deps required), with ECLIPSE_T overridden to a
real near-future moment actually covered by current archive (see TASKS.md
for how this value was picked):
    ECLIPSE_T=2026-07-25T15:00:00Z .venv/bin/python -m scripts.generate_tool3_manifest
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from src.config import DATA_RAW, get_model
from src.fetchers.base import eclipse_t, full_range_steps, nearest_step
from src.viz.tool1_renderer import OUTPUT_DIR, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool3_manifest")

FIELDS = ["total", "low", "mid", "high"]
# Renderings aren't final yet (styling/polish still pending) - cap this initial
# real-data pass to the most recent few runs per model so it stays fast. The
# real backfill across full run history happens once the rendering approach
# itself is settled, not now.
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


def main() -> None:
    target_valid_time = eclipse_t()
    log.info("target valid time (eclipse_t()) = %s", target_valid_time.isoformat())

    manifest_models = []
    for model_id, label in MODELS:
        model_config = get_model(model_id)
        run_inits = _archived_run_inits(model_id)
        log.info("%s: %d archived run(s)", model_id, len(run_inits))

        run_entries = []
        for run_init in run_inits:
            available = full_range_steps(model_config, run_init)
            offset_hours = (target_valid_time - run_init).total_seconds() / 3600
            hit = nearest_step(available, offset_hours)

            if hit is None:
                run_entries.append({
                    "run_init": _iso_z(run_init),
                    "covers": False,
                    "step": None,
                    "misalignment_h": None,
                    "images": None,
                    "has_data": None,
                })
                continue

            step, misalignment_h = hit
            images = {}
            has_data_by_field = {}
            for field in FIELDS:
                path, has_data = render_frame(model_id, run_init, step, field)
                images[field] = str(path.relative_to(OUTPUT_DIR)).replace("\\", "/")
                has_data_by_field[field] = has_data

            run_entries.append({
                "run_init": _iso_z(run_init),
                "covers": True,
                "step": step,
                "misalignment_h": round(misalignment_h, 2),
                "images": images,
                "has_data": has_data_by_field,
            })

        n_covering = sum(1 for r in run_entries if r["covers"])
        log.info("%s: %d/%d runs cover the target valid time", model_id, n_covering, len(run_entries))

        manifest_models.append({
            "id": model_id,
            "label": label,
            "runs": run_entries,
        })

    manifest = {
        "generated_at": _iso_z(datetime.now(UTC)),
        "target_valid_time": _iso_z(target_valid_time),
        "models": manifest_models,
    }
    manifest_path = OUTPUT_DIR / "tool3_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s", manifest_path)


if __name__ == "__main__":
    main()
