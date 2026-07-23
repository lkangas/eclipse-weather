"""Tool 1: batch-fetch + render real map frames for every wired model's
latest full-range run and write manifest.json describing what's available.

Image paths in the manifest are relative to manifest.json's own directory
(src/viz/tool1_renderer.py's OUTPUT_DIR) - not an absolute "/data/..." path -
so serving works regardless of exactly where that directory is mounted
(DATA_ROOT may not even be under the repo - see src/config.py).

Fetching: each model's own fetch_full_range() (one per fetcher module, see
src/fetchers/*.py) is dispatched here by the model's models.yaml `fetch:`
key - the same key src/fetchers/registry.py's FETCHERS dict is keyed by for
the eclipse archiver's fetch(), but fetch_full_range() itself isn't
registered there (it's Tool 1-only, so it doesn't belong in the shared
registry the scheduler also reads). Idempotent per fetcher (skips
already-downloaded files), so re-running this script only fetches whatever's
new since the last run.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.generate_tool1_manifest
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from src.config import get_model
from src.fetchers.base import full_range_steps, latest_available_run_init
from src.fetchers.dwd_bz2_fetcher import fetch_full_range as _fetch_full_range_http_bz2
from src.fetchers.ecmwf_opendata_fetcher import fetch_full_range as _fetch_full_range_ecmwf
from src.fetchers.herbie_fetcher import fetch_full_range as _fetch_full_range_herbie
from src.fetchers.meteofrance_fetcher import fetch_full_range as _fetch_full_range_http_grib
from src.viz.tool1_renderer import OUTPUT_DIR, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool1_manifest")

FIELDS = ["total", "low", "mid", "high"]
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

# models.yaml `fetch:` value -> this model's fetch_full_range() entry point.
# Every fetcher module that has one is listed here; a model whose fetch key
# isn't in this dict (shouldn't happen for anything in MODELS above) just
# skips the fetch step and renders whatever's already archived.
_FETCH_FULL_RANGE_BY_KEY = {
    "herbie": _fetch_full_range_herbie,
    "http_grib": _fetch_full_range_http_grib,
    "ecmwf-opendata": _fetch_full_range_ecmwf,
    "http_bz2": _fetch_full_range_http_bz2,
}


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _fetch_latest(model_id: str, model_config: dict, run_init: datetime) -> None:
    fetch_fn = _FETCH_FULL_RANGE_BY_KEY.get(model_config.get("fetch"))
    if fetch_fn is None:
        log.warning(
            "%s: no fetch_full_range() dispatch for fetch key %r, rendering whatever's "
            "already archived without fetching first",
            model_id, model_config.get("fetch"),
        )
        return
    log.info("%s: fetching full range for run_init=%s ...", model_id, run_init.isoformat())
    result = fetch_fn(model_id, model_config, run_init)
    log.info(
        "%s: fetch status=%s, %d file(s) written%s",
        model_id, result.status, len(result.files_written),
        f", error={result.error}" if result.error else "",
    )


def main() -> None:
    now = datetime.now(UTC)
    manifest_models = []

    for model_id, label in MODELS:
        model_config = get_model(model_id)
        run_init = latest_available_run_init(model_config, now)
        if run_init is None:
            log.warning("%s: no due run_init found (nothing published yet), skipping", model_id)
            continue

        _fetch_latest(model_id, model_config, run_init)

        steps = full_range_steps(model_config, run_init)
        log.info(
            "%s: run_init=%s, %d steps to render x %d fields",
            model_id, run_init.isoformat(), len(steps), len(FIELDS),
        )

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
                # e.g. arome_france's group files start at +1h, not +0h,
                # despite full_range_steps() assuming a step-0 field exists
                # (true for gfs, not for arome_france - see TASKS.md T34).
                # Exclude it rather than list a step that will always show
                # "(no data)" regardless of which quantity is selected.
                skipped.append(step)
                continue
            step_entries.append({
                "h": step,
                "valid": _iso_z(run_init + timedelta(hours=step)),
                "images": images,
                # Per-field flag, not just per-URL - images[field] always
                # exists (render_frame writes a "(no data)" placeholder PNG
                # even when has_data is False), so consumers need this to
                # tell a real map from a placeholder without inspecting
                # pixels themselves. Some models permanently lack specific
                # fields (arome_france/arpege_europe: no native total;
                # ecmwf_ens: no native low/mid/high) - not a bug, see
                # tool1_renderer.py's reader docstrings.
                "has_data": has_data_by_field,
            })
        if skipped:
            log.info(
                "%s: excluded %d step(s) with no real data in any field: %s",
                model_id, len(skipped), skipped,
            )

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
