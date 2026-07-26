"""Scan the rendered-PNG tree and write rendered_index.json: how much of the
whole job is ACTUALLY done on disk, per model.

Complements backfill_progress.json (written by scripts/render_backfill.py),
which only tracks what the CURRENT invocation has rendered - restart the
backfill and that counter resets to 0 even though the frames are still on
disk. This scan answers the different, restart-proof question: of every run
archived under data/raw/, how many have frames rendered for them?

Read-only: lists directories, opens nothing. Safe to run repeatedly while a
backfill is writing into the same tree.

Usage:
    .venv/bin/python -m scripts.scan_rendered            # scan once, write, exit
    .venv/bin/python -m scripts.scan_rendered --loop 30  # rescan every 30s until killed
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime

from src.config import DATA_RAW
from src.viz.tool1_renderer import OUTPUT_DIR, supported_fields

MODELS = [
    "gfs", "gefs_extended", "arome_france", "arpege_europe", "ecmwf_hres",
    "ecmwf_ens", "aifs_single", "aifs_ens", "icon_eu", "icon_global",
]

INDEX_PATH = OUTPUT_DIR / "rendered_index.json"

# Same convention as render_frame()'s output_path:
#   OUTPUT_DIR/{model}/{field}/{YYYYMMDDHH}_{step:03d}.png
_FRAME_RE = re.compile(r"^(\d{10})_(\d+)\.png$")
_RUN_DIR_RE = re.compile(r"^\d{10}$")


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _archived_run_count(model_id: str) -> int:
    d = DATA_RAW / model_id
    if not d.is_dir():
        return 0
    return sum(
        1 for p in d.iterdir()
        if p.is_dir() and _RUN_DIR_RE.match(p.name) and any(p.iterdir())
    )


def _scan_model(model_id: str) -> dict:
    """Per-model rendered totals. A run counts as rendered if ANY of its
    structurally-supported fields has at least one frame; fully_rendered
    additionally requires every supported field to have the same step count
    (a partial run - killed mid-render, or still in progress - fails that)."""
    runs: dict[str, dict[str, int]] = {}
    png_count = 0
    fields = supported_fields(model_id)
    for field in fields:
        field_dir = OUTPUT_DIR / model_id / field
        if not field_dir.is_dir():
            continue
        for entry in field_dir.iterdir():
            match = _FRAME_RE.match(entry.name)
            if match is None:
                continue
            png_count += 1
            runs.setdefault(match.group(1), {})
            runs[match.group(1)][field] = runs[match.group(1)].get(field, 0) + 1

    complete = 0
    for per_field in runs.values():
        if len(per_field) == len(fields) and len(set(per_field.values())) == 1:
            complete += 1

    return {
        "archived_runs": _archived_run_count(model_id),
        "rendered_runs": len(runs),
        "complete_runs": complete,
        "png_count": png_count,
        "fields": fields,
    }


def scan_once() -> dict:
    models = {m: _scan_model(m) for m in MODELS}
    index = {
        "updated_at": _iso_z(datetime.now(UTC)),
        "totals": {
            "archived_runs": sum(v["archived_runs"] for v in models.values()),
            "rendered_runs": sum(v["rendered_runs"] for v in models.values()),
            "complete_runs": sum(v["complete_runs"] for v in models.values()),
            "png_count": sum(v["png_count"] for v in models.values()),
        },
        "models": models,
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--loop", type=int, metavar="SECONDS",
        help="rescan every SECONDS until killed (default: scan once and exit)",
    )
    args = parser.parse_args()

    while True:
        index = scan_once()
        t = index["totals"]
        print(
            f"{index['updated_at']}  {t['rendered_runs']}/{t['archived_runs']} runs touched, "
            f"{t['complete_runs']} complete, {t['png_count']} PNGs",
            flush=True,
        )
        if not args.loop:
            return
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
