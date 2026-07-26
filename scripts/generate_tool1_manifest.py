"""Tool 1: batch-fetch + render real map frames for every wired model's
latest full-range run and write manifest.json describing what's available.

Image paths in the manifest are relative to manifest.json's own directory
(src/viz/frame_renderer.py's OUTPUT_DIR) - not an absolute "/data/..." path -
so serving works regardless of exactly where that directory is mounted
(DATA_ROOT may not even be under the repo - see src/config.py).

Fetching: dispatched via src/fetchers/registry.py's shared FETCHERS registry,
keyed by the model's models.yaml `fetch:` value - the same fetch() every
fetcher module registers for the eclipse archiver/scheduler (see TASKS.md's
2026-07-23 archiver-consolidation note: there used to be a separate
Tool 1-only fetch_full_range() per module, merged into fetch() the same day).
Idempotent per fetcher (skips already-downloaded files), so re-running this
script only fetches whatever's new since the last run.

Usage (inside Docker, GRIB deps required):
    .venv/bin/python -m scripts.generate_tool1_manifest
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from src import fetchers as _fetchers  # noqa: F401 - import for @register side-effects
from src.config import DATA_RAW, get_model
from src.fetchers.base import eclipse_t, full_range_steps, latest_available_run_init
from src.fetchers.registry import get_fetcher
from src.viz.frame_renderer import OUTPUT_DIR, render_frame

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool1_manifest")

FIELDS = ["total", "hml_composite", "prob_hml_composite"]
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


def _fetch_latest(model_id: str, model_config: dict, run_init: datetime) -> None:
    try:
        fetch_fn = get_fetcher(model_config["fetch"])
    except KeyError as e:
        log.warning(
            "%s: %s, rendering whatever's already archived without fetching first",
            model_id, e,
        )
        return
    log.info("%s: fetching full range for run_init=%s ...", model_id, run_init.isoformat())
    result = fetch_fn(model_id, model_config, run_init)
    log.info(
        "%s: fetch status=%s, %d file(s) written%s",
        model_id, result.status, len(result.files_written),
        f", error={result.error}" if result.error else "",
    )


def _step_entries(model_id: str, run_init: datetime, steps: list[int]) -> list[dict]:
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
            # frame_renderer.py's reader docstrings.
            "has_data": has_data_by_field,
        })
    if skipped:
        log.info(
            "%s %s: excluded %d step(s) with no real data in any field: %s",
            model_id, run_init.isoformat(), len(skipped), skipped,
        )
    return step_entries


def _reach(model_config: dict, run_init: datetime) -> datetime:
    """The furthest valid time this run publishes."""
    steps = full_range_steps(model_config, run_init)
    return run_init + timedelta(hours=max(steps)) if steps else run_init


def _longer_archived_run(
    model_id: str, model_config: dict, newest_run_init: datetime,
) -> datetime | None:
    """The most recent ALREADY-ARCHIVED run that reaches further in absolute
    time than the newest run does, or None if the newest run is also the
    furthest-reaching.

    Four models publish a different forecast length per cycle (models.yaml:
    gefs_extended 840h at 00Z vs 384h otherwise, ecmwf_hres 240/90,
    ecmwf_ens 360/144, icon_global 180/120), so "newest run" and
    "furthest-reaching run" are routinely different runs - for most of the
    day the newest gefs_extended run stops ~19 days short of where its last
    00Z run reaches. Tool 1 lists BOTH in that case, so the long-range view
    doesn't disappear for the 18 hours a day the short cycles are newest.

    Deliberately restricted to what's already on disk (no fetch): this is a
    second full run per affected model, and the backfill/scheduler has
    normally archived it already. A run that isn't archived simply isn't
    offered rather than doubling Tool 1's fetch cost.
    """
    model_dir = DATA_RAW / model_id
    if not model_dir.is_dir():
        return None
    newest_reach = _reach(model_config, newest_run_init)
    candidates = []
    for p in sorted(model_dir.iterdir(), reverse=True):
        if not p.is_dir() or not any(p.iterdir()):
            continue
        try:
            run_init = datetime.strptime(p.name, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if run_init >= newest_run_init:
            continue
        if _reach(model_config, run_init) > newest_reach:
            candidates.append(run_init)
    return max(candidates) if candidates else None


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
        manifest_models.append({
            # id is the ROW KEY (unique per manifest entry); model_id is the
            # real model. They differ only for the long-range companion entry
            # below, where one model contributes two rows - consumers keying
            # per-model behaviour (e.g. tool1_real.html's ensemble-only cloud
            # probability check) must use model_id, not id.
            "id": model_id,
            "model_id": model_id,
            "label": label,
            "run_init": _iso_z(run_init),
            "steps": _step_entries(model_id, run_init, steps),
        })
        log.info("%s: rendered %d steps", model_id, len(manifest_models[-1]["steps"]))

        long_run_init = _longer_archived_run(model_id, model_config, run_init)
        if long_run_init is not None:
            long_steps = full_range_steps(model_config, long_run_init)
            log.info(
                "%s: newest run stops at %s, adding longer archived run %s (reaches %s)",
                model_id, _iso_z(_reach(model_config, run_init)),
                long_run_init.isoformat(), _iso_z(_reach(model_config, long_run_init)),
            )
            manifest_models.append({
                "id": f"{model_id}__long",
                "model_id": model_id,
                "label": f"{label} · {long_run_init:%H}Z (+{max(long_steps)}h)",
                "run_init": _iso_z(long_run_init),
                "steps": _step_entries(model_id, long_run_init, long_steps),
            })
            log.info(
                "%s (long): rendered %d steps", model_id, len(manifest_models[-1]["steps"]),
            )

    # eclipse_t() (ECLIPSE_T env var, src/fetchers/base.py) rather than a
    # literal - CLAUDE.md's "never hardcode T" rule applies to the UI too, and
    # the browser has no other way to learn it. Consumed by tool1_real.html to
    # place its eclipse marker on the time axis; that marker is simply not
    # drawn if this key is absent (an older manifest).
    manifest = {
        "generated_at": _iso_z(now),
        "eclipse_t": _iso_z(eclipse_t()),
        "models": manifest_models,
    }
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s", manifest_path)


if __name__ == "__main__":
    main()
