"""Which forecast steps each raw file on disk carries.

The reclaim decision is made PER STEP (see src/pipeline/reclaim.py), so the
pipeline has to be able to answer "if I delete this file, which steps am I
throwing away?" without opening it - the file may be a 300 MB GRIB and the
answer has to be cheap and, above all, safe.

This is the one module that knows fetcher filename conventions. It does not
duplicate model metadata (CLAUDE.md hard constraint #2): cycles/steps/lengths
still come from models.yaml via full_range_steps(); what lives here is only
the mapping from a filename each fetcher chose to the step(s) inside it,
which models.yaml has no field for. src/viz/frame_renderer.py already
reconstructs these same filenames to READ them - this module is the delete
side of that same convention, and the two must be kept in step.

Fail-safe by construction: an unrecognised filename returns None, and
reclaim.py never deletes a file whose steps it cannot name. A new fetcher or
a renamed output therefore leaks disk (visible, recoverable) rather than
deleting something unrenderable (silent, unrecoverable).
"""

from __future__ import annotations

import re
from datetime import datetime

from src.fetchers.base import full_range_steps

# herbie_fetcher._output_filename(): f{step:03d}[_{member}]_{suffix}.grib2
#   gfs            f012_cloud.grib2
#   gefs_extended  f012_c00_levels.grib2 / f012_c00_total.grib2
_HERBIE_RE = re.compile(r"^f(\d{3})_.+\.grib2$")

# ecmwf_opendata_fetcher's three request builders:
#   ecmwf_hres  tcc_f012.grib2 / pl_f012.grib2
#   ecmwf_ens   tcc_f012.grib2
#   aifs_*      cloud_f012.grib2
_ECMWF_RE = re.compile(r"^(?:tcc|pl|cloud)_f(\d{3})\.grib2$")

# dwd_bz2_fetcher: filename taken straight from models.yaml's url_template,
#   icon-eu_europe_regular-lat-lon_single-level_2026072618_012_CLCL.grib2
#   icon_global_icosahedral_single-level_2026072618_012_CLCT.grib2
_ICON_RE = re.compile(r"_(\d{3})_[A-Z][A-Z0-9_]*\.grib2$")

# meteofrance_fetcher: one file per fixed GROUP WINDOW, not per step -
#   arome_france_SP2_00H06H.grib2   (steps 0..6, though +0h carries no data)
#   arpege_europe_SP2_000H012H.grib2
_MF_GROUP_RE = re.compile(r"_(\d{2,3})H(\d{2,3})H\.grib2$")

# cfgrib writes an index sidecar next to any GRIB it opens, so rendering
# leaves these all over data/raw/. They are pure derivatives of their parent
# and must go with it (and may go alone, once the parent already has).
_IDX_RE = re.compile(r"^(?P<parent>.+\.grib2)\.[0-9a-z]+\.idx$")

# Written by the archiver/pipeline itself, never by a fetcher. These are the
# run's bookkeeping and are what keeps already_fetched() true after every
# byte of raw data has been reclaimed - deleting them would make the run look
# unfetched and trigger a full re-download.
MARKER_FILES = frozenset(
    {".extracted", ".last_fetch_attempt", ".reclaimed.json", ".render_journal.json"}
)


def is_marker(name: str) -> bool:
    return name in MARKER_FILES or name.startswith(".")


def steps_in_file(
    model_id: str, model_config: dict, run_init: datetime, filename: str
) -> frozenset[int] | None:
    """The forecast-hour steps `filename` carries, or None if unknown.

    None is the safe answer and callers must treat it as "never reclaim".
    """
    idx = _IDX_RE.match(filename)
    if idx:
        return steps_in_file(model_id, model_config, run_init, idx.group("parent"))

    if is_marker(filename):
        return None

    for pattern in (_HERBIE_RE, _ECMWF_RE):
        m = pattern.match(filename)
        if m:
            return frozenset({int(m.group(1))})

    m = _ICON_RE.search(filename)
    if m:
        return frozenset({int(m.group(1))})

    m = _MF_GROUP_RE.search(filename)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        published = full_range_steps(model_config, run_init)
        return frozenset(s for s in published if lo <= s <= hi)

    return None


def group_files_by_step(
    model_id: str, model_config: dict, run_init: datetime, filenames: list[str]
) -> dict[int, list[str]]:
    """step -> the files carrying it. Files with unknown layout are omitted."""
    by_step: dict[int, list[str]] = {}
    for name in filenames:
        steps = steps_in_file(model_id, model_config, run_init, name)
        if not steps:
            continue
        for step in steps:
            by_step.setdefault(step, []).append(name)
    return by_step
