"""Temporary rendering-review grid: one representative frame per (model,
field) combo, reusing Tool 1's already-rendered PNGs (no re-rendering, no
GRIB deps needed here) - lets the rendering style get reviewed and iterated
on before committing to a real Tool 1/2/3 layout pass.

For each model/field, picks whichever already-rendered step has real data
and sits closest to +24h. A null cell means this model genuinely has no
native data for that field at any step (e.g. arome_france/arpege_europe's
"total", ecmwf_ens's low/mid/high/prob_cloud) - not a rendering bug.

Excludes aemet_harmonie: it's a pre-rendered color-ramp GeoTIFF with no
L/M/H breakdown and no reader in frame_renderer.py, so there is nothing
here yet to pick a frame from (see frame_renderer.py's own module docstring).

Usage: uv run python -m scripts.generate_review_grid
"""

from __future__ import annotations

import json

from src.config import DATA_ROOT

TOOL1_DIR = DATA_ROOT / "viz" / "tool1_frames"
FIELDS = ["total", "low", "mid", "high", "prob_cloud"]
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
TARGET_HOUR = 24


def _pick_step(steps: list[dict], field: str) -> dict | None:
    candidates = [s for s in steps if s["has_data"].get(field)]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s["h"] - TARGET_HOUR))


def main() -> None:
    manifest = json.loads((TOOL1_DIR / "manifest.json").read_text())
    by_id = {m["id"]: m for m in manifest["models"]}

    rows = []
    for model_id, label in MODELS:
        model = by_id.get(model_id)
        cells = {}
        for field in FIELDS:
            step = _pick_step(model["steps"], field) if model else None
            cells[field] = (
                {"image": step["images"][field], "h": step["h"], "valid": step["valid"]}
                if step
                else None
            )
        rows.append(
            {
                "id": model_id,
                "label": label,
                "run_init": model["run_init"] if model else None,
                "cells": cells,
            }
        )

    out = {"generated_at": manifest["generated_at"], "fields": FIELDS, "models": rows}
    out_path = TOOL1_DIR / "review_grid.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"wrote {out_path} ({len(rows)} models x {len(FIELDS)} fields)")


if __name__ == "__main__":
    main()
