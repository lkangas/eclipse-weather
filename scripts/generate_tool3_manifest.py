"""Tool 3: scan the already-rendered PNG tree and write the manifest of
each model's runs at the ONE step nearest eclipse_t() (see
src/fetchers/base.py - ECLIPSE_T env var, never hardcoded).

PURE CONSUMER - this script renders nothing and never touches data/raw/.
Rendering is a separate, earlier pass (scripts/render_backfill.py ->
src/viz/frame_renderer.py's render_run()), which writes every
step x structurally-supported field of every archived run to
OUTPUT_DIR/{model}/{field}/{YYYYMMDDHH}_{step:03d}.png. All this script does
is walk that tree, work out which step each run wants, and describe what it
finds. That decoupling is what makes the production fetch -> render
everything -> DELETE the raw GRIB pipeline possible (see CLAUDE.md's
disk-footprint note): a manifest script that still needed raw data would
break the moment raw data is deleted.

Unlike Tool 1 (every step of the LATEST run) or Tool 2 (every step of EVERY
run), Tool 3 lists ONE step per run - the whole point is comparing
models/runs at a single fixed valid time.

Which step that is comes from models.yaml alone (full_range_steps() +
nearest_step(), no raw data needed); whether that step is actually SHOWABLE
comes from whether its PNG exists on disk - see _run_entry() below.

Fetching: none. Rendering: none. Only a directory scan.

Usage (inside Docker - no raw data is read, but importing frame_renderer
still pulls in the GRIB/matplotlib stack), with ECLIPSE_T overridden to a
real near-future moment actually covered by current archive (see TASKS.md
for how this value was picked):
    ECLIPSE_T=2026-07-25T15:00:00Z .venv/bin/python -m scripts.generate_tool3_manifest
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from src.config import get_model
from src.fetchers.base import eclipse_t, full_range_steps, nearest_step
from src.viz.frame_renderer import OUTPUT_DIR, supported_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool3_manifest")

# No FIELDS list here on purpose - which fields a model can even have is
# supported_fields(model_id)'s answer, derived from models.yaml (CLAUDE.md's
# single-source-of-truth rule). It also does real work for this script beyond
# tidiness: the rendered tree still contains stale directories from earlier
# render passes that rendered all fields for all models unconditionally (e.g.
# arome_france/total, ecmwf_ens/hml_composite, gfs/prob_hml_composite - all
# full of "(no data)" placeholders), and filtering by supported_fields() is
# what keeps those out of the manifest.

# None = list every run that has rendered frames. The old cap (4) existed
# because THIS script did the rendering; now that rendering is a separate
# pass, capping would only hide frames that already exist on disk. Set to an
# int if the manifest ever needs trimming for size. (Tool 3 is one frame per
# run either way, so this manifest stays small regardless.)
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

    Kept as a local copy rather than shared with generate_tool2_manifest.py's
    identical helper, matching how _iso_z/_parse_run_init are already
    duplicated across all three manifest scripts (they are standalone
    entry points, not a package).
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


_NOT_COVERING = {"covers": False, "step": None, "misalignment_h": None, "images": None, "has_data": None}


def _run_entry(
    model_id: str, model_config: dict, run_init: datetime,
    by_step: dict[int, dict[str, str]], target_valid_time: datetime,
) -> dict:
    """One manifest entry for a single run.

    Two independent questions, deliberately answered from two different
    sources:
      1. WHICH step is nearest the target valid time - answered purely from
         models.yaml (full_range_steps() + nearest_step()), no raw data and
         no disk listing involved. Same step render_run() would have
         rendered, since it walks the same full_range_steps().
      2. Whether that step is actually SHOWABLE - answered from disk: does a
         PNG exist for it? covers: true is a promise to the browser that
         there is something to display, so a step that is theoretically
         published but has not been rendered (yet - a backfill in progress,
         or a fetch that failed for this run) must not claim it.

    has_data: this script can only see WHICH PNGs exist, never what's inside
    them, so has_data[field] is simply "a rendered frame exists on disk for
    this (run, step, field)". That is a deliberate redefinition of the flag
    the old render-in-place version produced (there it meant "the reader
    returned real data", the direct by-product of reading raw GRIB - which a
    pure disk scan cannot re-derive without the raw data this whole
    architecture is built to be able to delete).

    Why it's the right call anyway:
      - The browser (tool3_real.html) only ever uses the flag as
        `has_data[field] !== false` to decide whether to show images[field]
        at all; a missing field key and has_data:false take the identical
        "not available" code path, so file-existence is exactly the question
        it is really asking.
      - supported_fields() above already excludes every PERMANENT per-model
        field gap (arome_france/arpege_europe total, ecmwf_ens low/mid/high,
        prob for everything but aifs_ens) - i.e. the cases that matter, the
        ones tool3_real.html's KNOWN_FIELD_GAPS explains in prose.
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
    available = full_range_steps(model_config, run_init)
    offset_hours = (target_valid_time - run_init).total_seconds() / 3600
    hit = nearest_step(available, offset_hours)
    if hit is None:
        # Target is before this init, or beyond the run's max reach.
        return {"run_init": _iso_z(run_init), **_NOT_COVERING}

    step, misalignment_h = hit
    by_field = by_step.get(step, {})
    if not by_field:
        # The run reaches the target valid time, but nothing is rendered for
        # that step, so there is nothing to show. Reported as covers: false
        # (the browser's "not yet covering" state) rather than an entry
        # pointing at PNGs that don't exist.
        log.info(
            "%s %s: step +%dh not rendered, reporting as not covering",
            model_id, run_init.isoformat(), step,
        )
        return {"run_init": _iso_z(run_init), **_NOT_COVERING}

    fields = supported_fields(model_id)
    return {
        "run_init": _iso_z(run_init),
        "covers": True,
        "step": step,
        "misalignment_h": round(misalignment_h, 2),
        # images only carries fields that really have a file; has_data carries
        # every supported field so a consumer can tell "field supported but
        # not rendered" from "field not supported at all".
        "images": {f: by_field[f] for f in fields if f in by_field},
        "has_data": {f: f in by_field for f in fields},
    }


def _simulated_valid_time(now: datetime, days_ahead: int = 3) -> datetime:
    """The eclipse's own 18:30Z, but `days_ahead` days from now - a valid
    time current runs can actually reach, unlike the real eclipse (still
    weeks beyond every model's forecast horizon, so every run reports
    covers: false and Tool 3 has nothing to show).

    Same idea as CLAUDE.md's live-forward sim mode, but as a per-manifest
    target rather than an ECLIPSE_T override, so one manifest carries both
    and the UI can toggle without regenerating anything. Recomputed on every
    manifest generation, so it tracks "now" as the archive grows."""
    eclipse = eclipse_t()
    return (now + timedelta(days=days_ahead)).replace(
        hour=eclipse.hour, minute=eclipse.minute, second=0, microsecond=0,
    )


def main() -> None:
    now = datetime.now(UTC)
    targets = [
        {"key": "eclipse", "label": "Eclipse", "valid_time": eclipse_t()},
        {"key": "sim", "label": "Simulated (+3d)", "valid_time": _simulated_valid_time(now)},
    ]
    for t in targets:
        log.info("target %r valid time = %s", t["key"], t["valid_time"].isoformat())

    manifest_models = []
    for model_id, label in MODELS:
        model_config = get_model(model_id)
        index = _rendered_frames(model_id)
        # Ascending (oldest-first), same ordering contract as
        # generate_tool2_manifest.py's manifest.
        run_inits = sorted(index)
        if MAX_RUNS_PER_MODEL is not None:
            run_inits = run_inits[-MAX_RUNS_PER_MODEL:]

        if not run_inits:
            # Skipped rather than emitted with an empty runs list: every run
            # of a listed model is a row/gallery cell in tool3_real.html, and
            # a model with nothing rendered has no row to draw.
            log.info("%s: no rendered frames on disk, skipping", model_id)
            continue

        # The run LIST is target-independent (it's whatever is rendered on
        # disk), so each run carries one entry per target and toggling in the
        # UI only swaps which step/images are shown - rows and ticks stay put.
        run_entries = []
        for run_init in run_inits:
            entry = {"run_init": _iso_z(run_init), "by_target": {}}
            for t in targets:
                per_target = _run_entry(
                    model_id, model_config, run_init, index[run_init], t["valid_time"],
                )
                per_target.pop("run_init")  # already on the parent entry
                entry["by_target"][t["key"]] = per_target
            run_entries.append(entry)

        n_covering = sum(1 for r in run_entries if r["by_target"]["eclipse"]["covers"])
        n_covering_sim = sum(1 for r in run_entries if r["by_target"]["sim"]["covers"])
        log.info(
            "%s: %d/%d runs cover the simulated target", model_id, n_covering_sim, len(run_entries),
        )
        log.info(
            "%s: %d/%d run(s) cover the target valid time with rendered frames",
            model_id, n_covering, len(run_entries),
        )
        manifest_models.append({"id": model_id, "label": label, "runs": run_entries})

    manifest = {
        "generated_at": _iso_z(now),
        "targets": [
            {"key": t["key"], "label": t["label"], "valid_time": _iso_z(t["valid_time"])}
            for t in targets
        ],
        # The simulated target is the useful default TODAY - the real eclipse
        # is still beyond every model's horizon, so defaulting to it would
        # show an empty tool. Flip to "eclipse" once runs actually reach it.
        "default_target": "sim",
        "models": manifest_models,
    }
    manifest_path = OUTPUT_DIR / "tool3_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s (%d model(s))", manifest_path, len(manifest_models))


if __name__ == "__main__":
    main()
