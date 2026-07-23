"""Tool 1: batch-render real map frames for GFS + AROME's latest full-range
run and write manifest.json describing what's available.

Image paths in the manifest are relative to manifest.json's own directory
(src/viz/tool1_renderer.py's OUTPUT_DIR) - not an absolute "/data/..." path -
so serving works regardless of exactly where that directory is mounted
(DATA_ROOT may not even be under the repo - see src/config.py).

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.generate_tool1_manifest
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from src.config import get_model
from src.fetchers.base import full_range_steps, latest_available_run_init
from src.viz.tool1_renderer import OUTPUT_DIR, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool1_manifest")

FIELDS = ["total", "low", "mid", "high"]
MODELS = [("gfs", "GFS"), ("arome_france", "AROME France")]


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def main() -> None:
    now = datetime.now(UTC)
    manifest_models = []

    for model_id, label in MODELS:
        model_config = get_model(model_id)
        run_init = latest_available_run_init(model_config, now)
        if run_init is None:
            log.warning("%s: no due run_init found (nothing published yet), skipping", model_id)
            continue

        steps = full_range_steps(model_config, run_init)
        log.info("%s: run_init=%s, %d steps to render x %d fields", model_id, run_init.isoformat(), len(steps), len(FIELDS))

        step_entries = []
        for step in steps:
            images = {}
            for field in FIELDS:
                path = render_frame(model_id, run_init, step, field)
                images[field] = str(path.relative_to(OUTPUT_DIR)).replace("\\", "/")
            step_entries.append({
                "h": step,
                "valid": _iso_z(run_init + timedelta(hours=step)),
                "images": images,
            })

        manifest_models.append({
            "id": model_id,
            "label": label,
            "run_init": _iso_z(run_init),
            "steps": step_entries,
        })
        log.info("%s: rendered %d steps", model_id, len(step_entries))

    manifest = {"generated_at": _iso_z(now), "models": manifest_models}
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s", manifest_path)


if __name__ == "__main__":
    main()
