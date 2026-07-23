"""src/extract/meteofrance_extractor.py

Extractor for the two `fetch: http_grib` models (arpege_europe, arome_france)
in config/models.yaml -- both read the GRIB2 "group" files written by
src/fetchers/meteofrance_fetcher.py into data/raw/{model}/{initYYYYMMDDHH}/.

Meteo-France group files
------------------------
Unlike GFS/GEFS (one file per forecast-hour step -- see
grib_regular_extractor.py), Meteo-France's public OVH bucket splits every run
into a handful of files, each covering a FIXED lead-time WINDOW spanning many
steps and many unrelated params in one physical GRIB2 (e.g. arpege_europe's
`..._073H084H.grib2` covers steps 73-84h; arome_france's `..._37H42H.grib2`
covers 37-42h). This module must pick out the exact step matching each
archive target valid time's resolved step (via steps_for_run()), not just
open a group file and assume it holds a single step.

Real-file investigation (2026-07-22, inside Docker, against the real files
listed in this task -- via `eccodes.codes_grib_new_from_file` message
iteration first, then `cfgrib.open_datasets()`):

- `arpege_europe/2026072212/arpege_europe_SP2_073H084H.grib2`: 132 GRIB
  messages total. `lcc`/`mcc`/`hcc` (all typeOfLevel=surface, level=0) are
  present HOURLY across the whole window (steps 73..84), alongside `strd` --
  a finer cadence than models.yaml's `steps:` spec implies for this model
  past 48h (every_h: 3), but that's harmless: steps_for_run()'s resolved
  steps are always a SUBSET of what's actually published, so every step it
  asks for is present here. Other params in the same file (t, sp, blh, ssr,
  ...) are only present at the coarser 3-hourly cadence (75/78/81/84) --
  cfgrib can't merge those different-step-count fields into one Dataset, so
  `cfgrib.open_datasets()` (plural, same convention as
  grib_regular_extractor.py) is used; it auto-splits the file into
  hypercubes, and lcc/mcc/hcc land together in exactly one of them (confirmed
  live, not assumed -- see the module test run in this task's evidence).
- `arome_france/2026072200/arome_france_SP2_{37H42H,43H48H}.grib2`: same
  shape -- lcc/mcc/hcc/t/sp/blh/tirf land in one shared 6-step hypercube per
  file.
- Units confirmed live: both files' lcc/mcc/hcc carry GRIB attribute
  `units: "%"`, with real values in [~0, ~100] (arpege_europe step 78h: lcc
  min=1.0e-08 max=100.00000001 mean=24.44) -- confirms models.yaml's "native,
  package SP2, percent 0-100" note; no *100 scaling needed here (unlike
  ecmwf_hres/ecmwf_ens's [0,1] tcc fraction).
- Longitude convention: BOTH grids already come out of cfgrib in -180..180
  (arpege_europe: -32.0..42.0 covering EURAT; arome_france: -12.0..16.0
  covering EURW1S40) -- unlike the NOAA 0..360 grids in
  grib_regular_extractor.py, site longitudes need NO normalization here.

No native `total` cloud field
------------------------------
Package SP2 only documents `cloud.levels` (lcc/mcc/hcc) in models.yaml for
both models -- there is no `cloud.total` entry (total cover / NEBUL lives in
package SP1, which is not fetched here). cloud_total is therefore always
written as None; provenance describes the L/M/H fields that ARE written
(native), which is a different situation from AEMET's `total_only` (the
reverse: total present, levels absent).

`valid` on every emitted row is the archive TARGET valid time (one of
eclipse.archive_valid_hours_utc on eclipse_t()'s date), matching
grib_regular_extractor.py's convention -- required for the run-evolution
"d(Prog)/dt" view, which fixes valid time and slides run_init across runs.
The misalignment between target and actually-resolved step is available via
steps_for_run() itself if ever needed, and is intentionally not part of the
PointRow schema.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import cfgrib
import numpy as np
import xarray as xr

from src.config import DATA_RAW
from src.extract.base import PointRow, all_sample_points, file_fetched_at, nearest_gridpoint
from src.extract.registry import register
from src.fetchers.base import format_init_dir, steps_for_run

logger = logging.getLogger(__name__)

_SUPPORTED = {"arpege_europe", "arome_france"}
_LEVEL_VARS = ("lcc", "mcc", "hcc")
_ONE_HOUR = np.timedelta64(1, "h")


def _group_files(model_name: str, run_init: datetime) -> list[Path]:
    """Every non-empty *.grib2 group file archived for this run, in name
    order (matches the fetcher's group-window ordering, e.g. 000H012H before
    013H024H)."""
    d = DATA_RAW / model_name / format_init_dir(run_init)
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.grib2") if p.stat().st_size > 0)


def _cloud_dataset(path: Path) -> xr.Dataset | None:
    """The one cfgrib.open_datasets() hypercube (of several in a group file)
    that carries lcc/mcc/hcc together. Returns None if this file doesn't
    have one (shouldn't happen for a real SP2 file, but a fetch could have
    written a partial/corrupt file)."""
    try:
        datasets = cfgrib.open_datasets(str(path))
    except Exception:
        logger.exception("meteofrance_extractor: failed to open %s, skipping", path)
        return None
    for ds in datasets:
        if set(_LEVEL_VARS).issubset(ds.data_vars):
            return ds
    logger.warning("meteofrance_extractor: %s has no lcc/mcc/hcc hypercube, skipping", path)
    return None


def _step_hour_index(ds: xr.Dataset) -> dict[int, int]:
    """int forecast-hour offset -> positional index along ds's step dim."""
    return {int(td / _ONE_HOUR): i for i, td in enumerate(ds["step"].values)}


def _extract_meteofrance(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    steps = steps_for_run(model_config, run_init)
    covering = {vt: s for vt, s in steps.items() if s is not None}
    if not covering:
        logger.info(
            "%s run %s: not yet covering any archive valid time, nothing to extract",
            model_name,
            run_init.isoformat(),
        )
        return []

    files = _group_files(model_name, run_init)
    if not files:
        logger.warning(
            "%s run %s: no GRIB2 group files found under data/raw/%s/%s",
            model_name,
            run_init.isoformat(),
            model_name,
            format_init_dir(run_init),
        )
        return []

    # Open every group file once; keep only ones with a usable lcc/mcc/hcc
    # hypercube, alongside the set of forecast-hour steps it actually holds.
    opened: list[tuple[Path, xr.Dataset, dict[int, int]]] = []
    for path in files:
        ds = _cloud_dataset(path)
        if ds is not None:
            opened.append((path, ds, _step_hour_index(ds)))

    if not opened:
        logger.warning(
            "%s run %s: none of %d group file(s) yielded a usable lcc/mcc/hcc hypercube",
            model_name,
            run_init.isoformat(),
            len(files),
        )
        return []

    site_list = all_sample_points()
    rows: list[PointRow] = []

    for valid_iso, step_info in covering.items():
        step, _misalignment_h = step_info
        valid = datetime.fromisoformat(valid_iso)

        match = next(((p, ds, idx[step]) for p, ds, idx in opened if step in idx), None)
        if match is None:
            logger.warning(
                "%s run %s: step %dh (target valid=%s) not found in any archived "
                "group file %s, skipping this valid time",
                model_name,
                run_init.isoformat(),
                step,
                valid_iso,
                [p.name for p in files],
            )
            continue

        path, ds, step_idx = match
        at_step = ds.isel(step=step_idx)
        fetched_at = file_fetched_at(path)

        for site in site_list:
            point = nearest_gridpoint(at_step, site["lat"], site["lon"])
            values: dict[str, float | None] = {}
            for var in _LEVEL_VARS:
                val = float(point[var].values)
                values[var] = None if val != val else val  # NaN check, no extra import needed

            rows.append(
                PointRow(
                    model=model_name,
                    run_init=run_init,
                    member=-1,  # both arpege_europe and arome_france are deterministic
                    site=site["name"],
                    valid=valid,
                    cloud_low=values["lcc"],
                    cloud_mid=values["mcc"],
                    cloud_high=values["hcc"],
                    cloud_total=None,  # SP2 has no native total field (see module docstring)
                    provenance="native",
                    fetched_at=fetched_at,
                )
            )

    return rows


@register("http_grib")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Extract PointRows for arpege_europe or arome_france from already-
    archived GRIB2 group files under data/raw/{model_name}/{initYYYYMMDDHH}/.
    Returns [] (with a logged warning per gap) rather than raising when some
    steps/files are missing -- a partially-archived run should still yield
    whatever it can.
    """
    if model_name not in _SUPPORTED:
        raise KeyError(
            f"meteofrance_extractor does not know model '{model_name}'. "
            f"Supported: {sorted(_SUPPORTED)}"
        )
    return _extract_meteofrance(model_name, model_config, run_init)


if __name__ == "__main__":
    # Manual smoke test against whatever's already on disk, e.g.:
    #   .venv/Scripts/python.exe -m src.extract.meteofrance_extractor
    # (needs a working ecCodes/cfgrib install -- see this task's Docker note)
    import sys

    from src.config import get_model

    for _model_name in sorted(_SUPPORTED):
        _model_dir = DATA_RAW / _model_name
        if not _model_dir.exists():
            print(f"no data/raw/{_model_name}/ directory found, skipping", file=sys.stderr)
            continue
        _init_dirs = sorted(p for p in _model_dir.iterdir() if p.is_dir())
        if not _init_dirs:
            print(f"no data/raw/{_model_name}/<init>/ directories found, skipping", file=sys.stderr)
            continue

        _model_config = get_model(_model_name)
        for _init_dir in _init_dirs:
            _run_init = datetime.strptime(_init_dir.name, "%Y%m%d%H").replace(tzinfo=UTC)
            print(f"model={_model_name} run_init={_run_init.isoformat()}")
            _rows = extract(_model_name, _model_config, _run_init)
            print(f"  extracted {len(_rows)} rows")
            for _row in _rows:
                print(" ", _row)
