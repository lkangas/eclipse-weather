"""Tool 2: scan the already-rendered PNG tree and write the manifest of
every step of every run each model has frames for.

PURE CONSUMER - this script renders nothing and never touches data/raw/.
Rendering is a separate, earlier pass (scripts/render_backfill.py ->
src/viz/tool1_renderer.py's render_run()), which writes every
step x structurally-supported field of every archived run to
OUTPUT_DIR/{model}/{field}/{YYYYMMDDHH}_{step:03d}.png. All this script does
is walk that tree and describe what it finds. That decoupling is what makes
the production fetch -> render everything -> DELETE the raw GRIB pipeline
possible (see CLAUDE.md's disk-footprint note): a manifest script that still
needed raw data would break the moment raw data is deleted.

Unlike Tool 1 (every step of the LATEST run only) or Tool 3 (one step per
run, at a fixed valid time), Tool 2 lists every step of EVERY run that has
frames on disk - the run-over-run evolution view is the whole point.

Fetching: none. Rendering: none. Only a directory scan, so this is now cheap
to re-run as often as wanted (it used to be the expensive one, thousands of
renders per model).

Usage (inside Docker - no raw data is read, but importing tool1_renderer
still pulls in the GRIB/matplotlib stack):
    .venv/bin/python -m scripts.generate_tool2_manifest
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from src.viz.tool1_renderer import OUTPUT_DIR, supported_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool2_manifest")

# No FIELDS list here on purpose - which fields a model can even have is
# supported_fields(model_id)'s answer, derived from models.yaml (CLAUDE.md's
# single-source-of-truth rule). It also does real work for this script beyond
# tidiness: the rendered tree still contains stale directories from earlier
# render passes that rendered all fields for all models unconditionally (e.g.
# arome_france/total, ecmwf_ens/hml_composite, gfs/prob_hml_composite - all
# full of "(no data)" placeholders), and filtering by supported_fields() is
# what keeps those out of the manifest.

# None = list every run that has rendered frames. The old cap (4) existed
# because THIS script did the rendering and a full history was intractable
# here; now that rendering is a separate pass, capping would only hide frames
# that already exist on disk. Set to an int if the manifest ever needs
# trimming for size.
MAX_RUNS_PER_MODEL: int | None = None

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

# render_frame()'s own output_path convention:
#   OUTPUT_DIR/{model}/{field}/{format_init_dir(run_init)}_{step:03d}.png
# with format_init_dir() == "%Y%m%d%H" (src/fetchers/base.py). Step is
# matched with \d+ rather than \d{3} so a hypothetical >999h step still
# parses.
_FRAME_RE = re.compile(r"^(\d{10})_(\d+)\.png$")


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_run_init(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None


def _rendered_frames(model_id: str) -> dict[datetime, dict[int, dict[str, str]]]:
    """{run_init: {step: {field: image path}}} for every PNG already rendered
    for this model, across its structurally-supported fields only.

    Image paths are relative to manifest.json's own directory (OUTPUT_DIR),
    not absolute - same contract as generate_tool1_manifest.py, so serving
    works wherever DATA_ROOT happens to be mounted.

    Fields are NOT assumed to have identical step sets: a run can legitimately
    have e.g. total rendered for a step where hml_composite isn't (mid-render
    interruption, or a field that failed for that one step), so each step's
    field dict holds only what actually exists.
    """
    index: dict[datetime, dict[int, dict[str, str]]] = {}
    for field in supported_fields(model_id):
        field_dir = OUTPUT_DIR / model_id / field
        if not field_dir.is_dir():
            continue
        for entry in field_dir.iterdir():
            match = _FRAME_RE.match(entry.name)
            if match is None:
                continue  # stray/legacy file - ignore, same spirit as the T35/T36 empty-dir skip
            run_init = _parse_run_init(match.group(1))
            if run_init is None:
                continue
            step = int(match.group(2))
            by_step = index.setdefault(run_init, {})
            by_step.setdefault(step, {})[field] = f"{model_id}/{field}/{entry.name}"
    return index


def _step_entries(model_id: str, run_init: datetime, by_step: dict[int, dict[str, str]]) -> list[dict]:
    """One manifest entry per step that has at least one rendered frame,
    ascending by step.

    has_data: this script can only see WHICH PNGs exist, never what's inside
    them, so has_data[field] is simply "a rendered frame exists on disk for
    this (run, step, field)". That is a deliberate redefinition of the flag
    the old render-in-place version produced (there it meant "the reader
    returned real data", the direct by-product of reading raw GRIB - which a
    pure disk scan cannot re-derive without the raw data this whole
    architecture is built to be able to delete).

    Why it's the right call anyway:
      - The browser (tool2_real.html) only ever uses the flag as
        `has_data[field] !== false` to decide whether to show images[field]
        at all; a missing field key and has_data:false take the identical
        "not available" code path, so file-existence is exactly the question
        it is really asking.
      - supported_fields() above already excludes every PERMANENT per-model
        field gap (arome_france/arpege_europe total, ecmwf_ens hml_composite,
        prob_hml_composite for everything but aifs_ens) - i.e. the cases that
        matter, the ones tool2_real.html's KNOWN_FIELD_GAPS explains in prose.
      - The renderer is intended (agreed, not yet implemented) to stop writing
        a placeholder PNG at all when there's no real data, at which point
        "file exists" == "has real data" exactly.
    Until that renderer change lands, a "(no data)" placeholder PNG for a step
    a model never actually published (e.g. arome_france's +0h, TASKS.md T34)
    is indistinguishable from a real map here and will be listed as
    has_data: true. That is the accepted cost; nothing downstream breaks, the
    frame just shows the renderer's own red "(no data)" text instead of the
    prose explanation.
    """
    fields = supported_fields(model_id)
    entries = []
    # A step with no frame in ANY field simply never enters the index, so the
    # old "exclude steps with no real data in any field" filter is structural
    # here rather than an explicit check.
    for step in sorted(by_step):
        by_field = by_step[step]
        entries.append({
            "h": step,
            "valid": _iso_z(run_init + timedelta(hours=step)),
            # images only carries fields that really have a file; has_data
            # carries every supported field so a consumer can tell "field
            # supported but not rendered" from "field not supported at all".
            "images": {f: by_field[f] for f in fields if f in by_field},
            "has_data": {f: f in by_field for f in fields},
        })
    return entries


def main() -> None:
    manifest_models = []
    for model_id, label in MODELS:
        index = _rendered_frames(model_id)
        # Ascending (oldest-first) - the order tool2_real.html's own row
        # building depends on.
        run_inits = sorted(index)
        if MAX_RUNS_PER_MODEL is not None:
            run_inits = run_inits[-MAX_RUNS_PER_MODEL:]

        if not run_inits:
            # Skipped rather than emitted with an empty runs list - a model
            # with nothing rendered is only a dead "(0 runs archived)" entry
            # in tool2_real.html's model picker.
            log.info("%s: no rendered frames on disk, skipping", model_id)
            continue

        run_entries = [
            {
                "run_init": _iso_z(run_init),
                "steps": _step_entries(model_id, run_init, index[run_init]),
            }
            for run_init in run_inits
        ]
        log.info(
            "%s: %d run(s), %d step(s) total across fields %s",
            model_id, len(run_entries), sum(len(r["steps"]) for r in run_entries),
            supported_fields(model_id),
        )
        manifest_models.append({"id": model_id, "label": label, "runs": run_entries})

    # Written once, at the end. The old version flushed after every run
    # because rendering could take hours and a partial manifest beat a 404;
    # a pure directory scan finishes in well under a second, so incremental
    # writes would only add a window where a truncated manifest could
    # overwrite a complete one.
    manifest = {"generated_at": _iso_z(datetime.now(UTC)), "models": manifest_models}
    manifest_path = OUTPUT_DIR / "tool2_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s (%d model(s))", manifest_path, len(manifest_models))


if __name__ == "__main__":
    main()
