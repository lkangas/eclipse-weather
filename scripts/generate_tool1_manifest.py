"""Tool 1: scan the already-rendered PNG tree and write the manifest of each
model's most recent run - plus, when the newest run falls short, the
furthest-reaching one alongside it.

PURE CONSUMER - this script renders nothing, fetches nothing, and never
touches data/raw/. Rendering is a separate, earlier pass
(scripts/render_backfill.py, or the archiver itself -> frame_renderer's
render_run()), which writes every step x structurally-supported field of
every archived run to
OUTPUT_DIR/{model}/{field}/{YYYYMMDDHH}_{step:03d}.png. All this script does
is walk that tree and describe what it finds. That decoupling is what makes
the production fetch -> render everything -> DELETE the raw GRIB pipeline
possible (see CLAUDE.md's disk-footprint note): a manifest script that still
needed raw data would break the moment raw data is deleted.

This script used to fetch and render inline, which is why it took minutes
while tool 2/3's equivalents take seconds. It no longer does either; the
archiver keeps runs topped up and the backfill renders them.

Unlike Tool 2 (every step of EVERY run) or Tool 3 (one step per run at a
fixed valid time), Tool 1 shows the CURRENT state of each model: its newest
run, every step.

Usage (inside Docker - no raw data is read, but importing frame_renderer
still pulls in the GRIB/matplotlib stack):
    .venv/bin/python -m scripts.generate_tool1_manifest
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta

from src.fetchers.base import eclipse_t
from src.viz.frame_renderer import OUTPUT_DIR, supported_fields

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_tool1_manifest")

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

    Image paths stay relative to manifest.json's own directory (OUTPUT_DIR),
    so serving works wherever DATA_ROOT happens to be mounted. Same local
    helper as generate_tool{2,3}_manifest.py - these are standalone entry
    points, not a package.
    """
    index: dict[datetime, dict[int, dict[str, str]]] = {}
    for field in supported_fields(model_id):
        field_dir = OUTPUT_DIR / model_id / field
        if not field_dir.is_dir():
            continue
        for entry in field_dir.iterdir():
            match = _FRAME_RE.match(entry.name)
            if match is None:
                continue  # stray/legacy file - ignore
            run_init = _parse_run_init(match.group(1))
            if run_init is None:
                continue
            step = int(match.group(2))
            by_step = index.setdefault(run_init, {})
            by_step.setdefault(step, {})[field] = f"{model_id}/{field}/{entry.name}"
    return index


def _reach(run_init: datetime, by_step: dict[int, dict[str, str]]) -> datetime:
    """Furthest valid time this run has an ACTUALLY RENDERED frame for.

    Deliberately measured from disk rather than from models.yaml's declared
    cycle length. A 00Z gefs_extended run is configured to reach 840h, but
    NOAA publishes that extended tail ~25-27h after init, so for most of a
    day the run exists with only its first 384h. Ranking by the declared
    reach picked such a run as "the long one" and produced a companion row
    that reached LESS far than the newest run - see _longest_run below.
    """
    return run_init + timedelta(hours=max(by_step)) if by_step else run_init


def _step_entries(run_init: datetime, by_step: dict[int, dict[str, str]], fields: list[str]) -> list[dict]:
    """One entry per rendered step, ascending. A step with no frame in any
    field simply never entered the index, so the old "exclude steps with no
    real data" filter is structural here rather than an explicit check.

    has_data now means "a frame exists on disk", which is exact: the renderer
    no longer writes a placeholder when there is no data (see
    frame_renderer.render_frame), so a file's presence IS the signal. images
    carries only fields with a real file; has_data carries every supported
    field so a consumer can tell "supported but not rendered" from "not
    supported at all".
    """
    return [
        {
            "h": step,
            "valid": _iso_z(run_init + timedelta(hours=step)),
            "images": {f: by_step[step][f] for f in fields if f in by_step[step]},
            "has_data": {f: f in by_step[step] for f in fields},
        }
        for step in sorted(by_step)
    ]


def _longest_run(index: dict, newest: datetime) -> datetime | None:
    """The rendered run that reaches furthest, if that is not the newest one.

    Four models publish a different forecast length per cycle (models.yaml:
    gefs_extended 840h at 00Z vs 384h otherwise, ecmwf_hres 240/90,
    ecmwf_ens 360/144, icon_global 180/120), so for most of the day the
    newest run stops well short of where the last long-cycle run reaches,
    and a "latest run only" view loses the long-range picture entirely.
    Tool 1 lists both in that case - and only then, so when the newest run
    IS the long-cycle one there is a single row, not a duplicate.
    """
    newest_reach = _reach(newest, index[newest])
    candidates = [r for r in index if r != newest and _reach(r, index[r]) > newest_reach]
    return max(candidates, key=lambda r: (_reach(r, index[r]), r)) if candidates else None


def main() -> None:
    manifest_models = []
    for model_id, label in MODELS:
        index = _rendered_frames(model_id)
        if not index:
            log.info("%s: no rendered frames on disk, skipping", model_id)
            continue
        fields = supported_fields(model_id)

        newest = max(index)
        manifest_models.append({
            # id is the ROW KEY (unique per manifest entry); model_id is the
            # real model. They differ only for the long-range companion entry
            # below, where one model contributes two rows - consumers keying
            # per-model behaviour (newest_runs.html's ensemble-only cloud
            # probability lock) must use model_id, not id.
            "id": model_id,
            "model_id": model_id,
            "label": label,
            "run_init": _iso_z(newest),
            "steps": _step_entries(newest, index[newest], fields),
        })
        log.info(
            "%s: newest run %s, %d step(s), reaches %s",
            model_id, _iso_z(newest), len(index[newest]), _iso_z(_reach(newest, index[newest])),
        )

        longest = _longest_run(index, newest)
        if longest is not None:
            manifest_models.append({
                "id": f"{model_id}__long",
                "model_id": model_id,
                "label": f"{label} · {longest:%d}d {longest:%H}Z",
                "run_init": _iso_z(longest),
                "steps": _step_entries(longest, index[longest], fields),
            })
            log.info(
                "%s: newest stops at %s, adding longer run %s (reaches %s)",
                model_id, _iso_z(_reach(newest, index[newest])),
                _iso_z(longest), _iso_z(_reach(longest, index[longest])),
            )

    manifest = {
        "generated_at": _iso_z(datetime.now(UTC)),
        # eclipse_t() (ECLIPSE_T env var) rather than a literal - CLAUDE.md's
        # "never hardcode T" rule applies to the UI too, and the browser has
        # no other way to learn it. newest_runs.html places its eclipse marker
        # and its axis floor from this; both are simply absent without it.
        "eclipse_t": _iso_z(eclipse_t()),
        "models": manifest_models,
    }
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s (%d row(s))", manifest_path, len(manifest_models))


if __name__ == "__main__":
    main()
