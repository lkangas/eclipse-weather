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
from pathlib import Path

from src.config import DATA_RAW
from src.viz.frame_renderer import OUTPUT_DIR, supported_fields

MODELS = [
    "gfs", "gefs_extended", "arome_france", "arpege_europe", "ecmwf_hres",
    "ecmwf_ens", "aifs_single", "aifs_ens", "icon_eu", "icon_global",
]

INDEX_PATH = OUTPUT_DIR / "rendered_index.json"

# A snapshot cannot answer "are we catching up or falling behind", which is the
# only question that matters while a backlog is draining - so each scan also
# appends one compact line here. JSONL rather than a rewritten array so a
# concurrent reader can never catch a half-written file, and trimmed to the
# most recent HISTORY_MAX_ROWS so it stays a small, servable file forever.
HISTORY_PATH = OUTPUT_DIR / "rendered_history.jsonl"
HISTORY_MAX_ROWS = 2880  # 24h at one scan per 30s

# Same convention as render_frame()'s output_path:
#   OUTPUT_DIR/{model}/{field}/{YYYYMMDDHH}_{step:03d}.png
_FRAME_RE = re.compile(r"^(\d{10})_(\d+)\.png$")
_RUN_DIR_RE = re.compile(r"^\d{10}$")

# The render worker drops this in a run directory once it has processed the run
# (src/scheduler/run.py's _RENDER_MARKER). Counting it is what distinguishes
# "the worker is stuck" from "the worker is working through runs that were
# already rendered, so the completed-runs total cannot move yet" - the second
# looks identical to the first if you only watch frame counts.
_WORKER_MARKER = ".last_render"


def _iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _worker_checked_count(model_id: str) -> int:
    d = DATA_RAW / model_id
    if not d.is_dir():
        return 0
    return sum(
        1 for p in d.iterdir()
        if p.is_dir() and _RUN_DIR_RE.match(p.name) and (p / _WORKER_MARKER).exists()
    )


def _has_raw_data(run_dir: Path) -> bool:
    """At least one real data file - not just bookkeeping dotfiles.

    A failed fetch still leaves the run directory behind holding .extracted /
    .last_fetch_attempt markers, so "the directory is non-empty" counted those
    as archived. The render worker skips them (raw_count == 0), so they were
    permanently-unrenderable backlog: the gap could never reach zero and the
    newest-first strip showed holes that no amount of rendering would fill.
    Two such runs exist in the renderable set today - icon_eu and icon_global
    2026-07-25 12Z, whose DWD fetch errored and which are now past DWD's ~24h
    retention, so they are unrecoverable rather than pending.
    """
    return any(p.is_file() and not p.name.startswith(".") for p in run_dir.iterdir())


def _archived_run_names(model_id: str) -> list[str]:
    """Archived run directories, NEWEST FIRST - the order the render workers
    themselves consume the queue in."""
    d = DATA_RAW / model_id
    if not d.is_dir():
        return []
    return sorted(
        (p.name for p in d.iterdir()
         if p.is_dir() and _RUN_DIR_RE.match(p.name) and _has_raw_data(p)),
        reverse=True,
    )


def _archived_run_count(model_id: str) -> int:
    return len(_archived_run_names(model_id))


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

    def _state(name: str) -> str:
        per_field = runs.get(name)
        if not per_field:
            return "none"
        if len(per_field) == len(fields) and len(set(per_field.values())) == 1:
            return "complete"
        return "partial"

    complete = 0
    for per_field in runs.values():
        if len(per_field) == len(fields) and len(set(per_field.values())) == 1:
            complete += 1

    # Per-run state, newest first. A single percentage cannot tell "the newest
    # run is rendered and the tail is missing" from the reverse, and the newest
    # runs are the ones the tools actually show - so the progress bar has to be
    # ordered, not aggregated.
    archived = _archived_run_names(model_id)
    return {
        "runs": [{"init": name, "state": _state(name)} for name in archived],
        "newest_unrendered": next(
            (name for name in archived if _state(name) != "complete"), None
        ),
        "archived_runs": len(archived),
        "worker_checked_runs": _worker_checked_count(model_id),
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
            "worker_checked_runs": sum(v["worker_checked_runs"] for v in models.values()),
            "rendered_runs": sum(v["rendered_runs"] for v in models.values()),
            "complete_runs": sum(v["complete_runs"] for v in models.values()),
            "png_count": sum(v["png_count"] for v in models.values()),
        },
        "models": models,
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")
    _append_history(index)
    return index


def _append_history(index: dict) -> None:
    """One line per scan: just the totals, which is all a trend needs."""
    t = index["totals"]
    row = {
        "at": index["updated_at"],
        "archived": t["archived_runs"],
        "rendered": t["rendered_runs"],
        "complete": t["complete_runs"],
        "checked": t["worker_checked_runs"],
        "pngs": t["png_count"],
    }
    try:
        rows = (HISTORY_PATH.read_text(encoding="utf-8").splitlines()
                if HISTORY_PATH.exists() else [])
    except OSError:
        rows = []
    rows.append(json.dumps(row, separators=(",", ":")))
    HISTORY_PATH.write_text("\n".join(rows[-HISTORY_MAX_ROWS:]) + "\n", encoding="utf-8")


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
