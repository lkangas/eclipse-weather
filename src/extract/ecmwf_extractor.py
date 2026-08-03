"""Extractor for the `ecmwf-opendata` fetch family: ecmwf_hres, ecmwf_ens,
aifs_single, aifs_ens. Reads the raw GRIB2 files
src/fetchers/ecmwf_opendata_fetcher.py writes under
data/raw/{model}/{initYYYYMMDDHH}/:

    ecmwf_hres:  tcc_f{step:03d}.grib2 (native total, entireAtmosphere)
                 pl_f{step:03d}.grib2  (q, t, z on 6 pressure levels)
    ecmwf_ens:   tcc_f{step:03d}.grib2 (native total, per pf member)
    aifs_single: cloud_f{step:03d}.grib2 (native tcc/lcc/mcc/hcc)
    aifs_ens:    cloud_f{step:03d}.grib2 (native tcc/lcc/mcc/hcc, per member)

Provenance / row-shape design (CLAUDE.md Hard Constraint #3 + PointRow's
one-`provenance`-string-per-row schema)
---------------------------------------------------------------------------
ecmwf_hres has a native TOTAL cloud field (tcc, models.yaml-confirmed) but
NO native low/mid/high split. A humidity-derived L/M/H estimate was
implemented and wired in here (via src/derive/humidity_to_cloud.py's q+t ->
RH -> cloud-fraction pipeline on the model's pressure-level file), but is
deliberately NOT used: checked against ecmwf_hres's own native total (the
one real field this model has to validate against), the derived bands
combined via random overlap correlate only ~0.35 with native total (mean
|diff| ~14pp, max ~97pp) - the RHc thresholds were tuned on a single GFS
calibration sample (T22) and were never actually validated against HRES
itself. Per explicit user direction, ecmwf_hres now emits exactly ONE
PointRow per (site, valid time), same shape as ecmwf_ens:

  provenance="native": cloud_total populated (tcc, scaled to percent),
  cloud_low/mid/high left None.

pl_f*.grib2 (q/t/z on 6 pressure levels) is still fetched - Tool 3's
cloud_field_comparison.py still uses derive_cloud_fractions on it - but
nothing in this extractor reads it anymore.

(Historical: this row's docstring previously described a SECOND
provenance="derived" row per (site, valid time) built from
_hres_derived_rows(), now removed. The "Known real-data gap" note further
below about a stale-pl-file idempotency bug is accordingly moot for this
extractor's own output, though it may still matter to cloud_field_comparison.py's
still-live use of the same derivation.)

This module never fabricates a derived total or a native L/M/H split that
doesn't exist in the raw data.

ecmwf_ens / aifs_single / aifs_ens have no derive step -- every field in
their row(s) is genuinely native, so they get exactly one row per (site,
valid time, member), provenance="native".

Member conventions
------------------
- ecmwf_hres, aifs_single: deterministic, member=-1.
- ecmwf_ens: real archived data (live-checked 2026-07-22) carries pf
  members only (1..50), zero cf -- matches models.yaml's note that the
  classic ENS open-data feed currently ships no control member. member =
  the real GRIB `number` value. Written generically (see _iter_members)
  so a cf message reappearing later is still handled (member=0), not
  hard-assumed absent forever.
- aifs_ens: real archived data carries both cf (1 member) and pf (50
  members) = 51 total, confirmed live 2026-07-22. Convention adopted here
  (models.yaml does not itself specify a cf-numbering convention):
  cf -> member=0, pf -> member=<its real GRIB `number`, 1..50>.

Grid / units
------------
- Grid is regular lat/lon, 0.25 deg (721x1440), latitude descending
  (90..-90), longitude ALREADY -180..180 (not 0-360) -- confirmed by
  opening a real archived file, so sites.yaml's negative longitudes need
  no wraparound conversion before nearest_gridpoint().
- Percent scaling is read out of models.yaml's own `cloud.total.units` /
  `cloud.levels.units` fields (fraction_0_1 -> x100, percent_0_100 -> x1)
  rather than hardcoded per model name -- see _percent_scale(). This is
  CLAUDE.md Hard Constraint #2 applied to units specifically: hres/ens's
  native tcc is a live-confirmed [0,1] fraction; aifs's native
  tcc/lcc/mcc/hcc are live-confirmed already 0-100 (models.yaml notes on
  each entry, dated 2026-07-22 T20 build). If a units field is ever
  missing/renamed in models.yaml this raises loudly rather than silently
  guessing a scale factor.

Known real-data gap found while building/testing this module (2026-07-22)
---------------------------------------------------------------------------
The already-archived data/raw/ecmwf_hres/2026072206/pl_f0*.grib2 files are
STALE: each contains only z+q (12 messages: 6 levels x 2 params) -- NO 't'
at all -- even though the current ecmwf_opendata_fetcher.py source requests
param=["q","t","z"] and its own module docstring says a missing-'t' bug was
found and fixed. Root cause, confirmed live during this build: the
fetcher's idempotency check (`if target.exists() and target.stat().st_size
> 0: skip`) means these specific files were fetched once, early -- before
"t" was added to the request -- and have simply never been re-fetched
since. A fresh live re-request of the IDENTICAL (date=2026-07-22, time=6,
step=81, levtype=pl, levelist=[1000,925,850,700,500,300], param=[q,t,z])
request during this module's build succeeded and DID return 't' (18
messages: 6 levels x 3 params) -- so this is a stale-file/idempotency-skip
problem, not an upstream data-availability problem. src/fetchers/* is
off-limits for this task, so this is flagged here rather than fixed there;
whoever owns the fetcher should purge (or otherwise force a re-fetch of)
every archived ecmwf_hres pl_f*.grib2 file to pick up 't'.

This module handles the gap defensively rather than crashing on it: a
missing 't' (or any other derive_cloud_fractions KeyError/ValueError) is
caught and logged per-step in _hres_derived_rows -- the native-tcc row for
that (site, valid time) is still written; only the derived row is skipped
for that step. See this module's test evidence (reported by the
implementing agent) for a real derived-cloud run against a freshly
re-fetched pl file that does have 't'.
"""

import logging
from datetime import datetime
from pathlib import Path

import cfgrib
import numpy as np
import xarray as xr

from src.extract.base import PointRow, all_sample_points, file_fetched_at, nearest_gridpoint
from src.extract.registry import register
from src.fetchers.base import all_valid_times_for_run, raw_output_dir

log = logging.getLogger(__name__)

# GRIB shortName -> PointRow band field, for AIFS's native lcc/mcc/hcc.
# A naming-convention decode (l/m/h = low/mid/high), not model metadata, so
# it doesn't duplicate anything models.yaml itself defines.
_SHORTNAME_TO_BAND = {"lcc": "low", "mcc": "mid", "hcc": "high"}

# models.yaml's cloud.total/.levels `units:` value -> multiplier to reach
# PERCENT (0-100), the schema's required unit -- see module docstring.
_UNITS_TO_PERCENT_SCALE = {"fraction_0_1": 100.0, "percent_0_100": 1.0}


def _percent_scale(cloud_field_config: dict, field_label: str) -> float:
    """Percent-scale multiplier for one models.yaml cloud.total/cloud.levels
    block, read from its own `units:` key -- see module docstring."""
    units = cloud_field_config.get("units")
    if units not in _UNITS_TO_PERCENT_SCALE:
        raise ValueError(
            f"models.yaml's cloud.{field_label}.units is {units!r}, not one of "
            f"{sorted(_UNITS_TO_PERCENT_SCALE)} -- refusing to guess a percent scale factor."
        )
    return _UNITS_TO_PERCENT_SCALE[units]


def _valid_times_by_step(model_config: dict, run_init: datetime) -> dict[int, list[datetime]]:
    """Reduce all_valid_times_for_run()'s valid_time -> (step, misalignment)
    map down to step -> [valid_times]. Widened 2026-08-03 from the original
    3-eclipse-hour-only steps_for_run() to every step this run publishes -
    see that function's own docstring. The de-dup-by-step grouping stays
    (harmless/rare now that valid times are each step's own real one rather
    than several archive targets nearest-mapping to the same step, but cheap
    to keep either way)."""
    steps_map = all_valid_times_for_run(model_config, run_init)
    by_step: dict[int, list[datetime]] = {}
    for valid_iso, resolved in steps_map.items():
        if resolved is None:
            continue
        step, _misalignment_h = resolved
        by_step.setdefault(step, []).append(datetime.fromisoformat(valid_iso))
    return by_step


def _iter_members(
    path: Path, shortname: str, var: str | None = None
) -> list[tuple[int, xr.DataArray]]:
    """Every (member, 2-D DataArray) pair for one GRIB2 shortName in `path`.

    `var` is the name cfgrib gives the decoded variable, when it differs from
    the GRIB shortName used to select the messages. For every cloud field here
    the two coincide (shortName "tcc" decodes as variable "tcc"), which is why
    one argument sufficed originally - but 2 m temperature has shortName "2t"
    and decodes as "t2m", so filtering and indexing need separate names.
    Defaults to `shortname`, leaving every existing caller unchanged.

    member = -1 for a field with no ensemble `number` key at all (a true
    deterministic run: ecmwf_hres, aifs_single).
    member = the real GRIB `number` value otherwise. cfgrib splits an
    ensemble file's control member (no perturbationNumber set -> `number`
    surfaces as a scalar coord, not a dimension) from its perturbed members
    (`number` as a real dimension) into separate hypercubes; both are read
    and yielded here. Real shapes live-confirmed 2026-07-22: ecmwf_ens
    ships pf members 1..50 only (zero cf, matches models.yaml's note);
    aifs_ens ships both -- 1 cf (scalar number=0) + 50 pf (number 1..50).
    """
    dsets = cfgrib.open_datasets(
        str(path), backend_kwargs={"filter_by_keys": {"shortName": shortname}}
    )
    out: list[tuple[int, xr.DataArray]] = []
    for ds in dsets:
        da = ds[var or shortname]
        if "number" in da.dims:
            for n in da["number"].values:
                out.append((int(n), da.sel(number=int(n))))
        elif "number" in da.coords:
            out.append((int(da["number"].values), da))
        else:
            out.append((-1, da))
    return out


def _value_at(da: xr.DataArray, lat: float, lon: float) -> float | None:
    point = nearest_gridpoint(da, lat, lon)
    value = float(point.values)
    return None if np.isnan(value) else value


def _native_total_only_rows(
    model_name: str,
    run_init: datetime,
    tcc_path: Path,
    total_shortname: str,
    scale: float,
    valid_times: list[datetime],
    site_list: list[dict],
) -> list[PointRow]:
    """provenance="native" rows carrying cloud_total only (cloud_low/mid/high
    left None) -- used by ecmwf_hres (member -1) and ecmwf_ens (per pf
    member). AIFS's native total is handled by _aifs_rows() instead, in the
    SAME row as its native L/M/H (all four fields are genuinely native
    there, so they belong together, unlike hres's native-total-only case)."""
    if not tcc_path.exists():
        log.warning(
            "%s %s: tcc file missing, skipping: %s", model_name, run_init.isoformat(), tcc_path
        )
        return []
    fetched_at = file_fetched_at(tcc_path)
    rows: list[PointRow] = []
    for member, da in _iter_members(tcc_path, total_shortname):
        for site in site_list:
            value = _value_at(da, site["lat"], site["lon"])
            total = None if value is None else value * scale
            for valid in valid_times:
                rows.append(
                    PointRow(
                        model=model_name,
                        run_init=run_init,
                        member=member,
                        site=site["name"],
                        valid=valid,
                        cloud_low=None,
                        cloud_mid=None,
                        cloud_high=None,
                        cloud_total=total,
                        provenance="native",
                        fetched_at=fetched_at,
                    )
                )
    return rows


def _extract_total_only(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Shared by ecmwf_hres and ecmwf_ens - both have a native total cloud
    field and nothing else worth writing here (ecmwf_hres's humidity-derived
    L/M/H is deliberately not used, see module docstring; ecmwf_ens simply
    has no native L/M/H at all)."""
    out_dir = raw_output_dir(model_name, run_init)
    by_step = _valid_times_by_step(model_config, run_init)
    total_shortname = model_config["cloud"]["total"]["param"]
    scale = _percent_scale(model_config["cloud"]["total"], "total")
    site_list = all_sample_points()

    rows: list[PointRow] = []
    for step, valid_times in by_step.items():
        tcc_path = out_dir / f"tcc_f{step:03d}.grib2"
        rows.extend(
            _native_total_only_rows(
                model_name, run_init, tcc_path, total_shortname, scale, valid_times, site_list
            )
        )
    return rows


def _aifs_rows(
    model_name: str,
    run_init: datetime,
    cloud_path: Path,
    total_shortname: str,
    total_scale: float,
    level_shortnames: list[str],
    level_scale: float,
    valid_times: list[datetime],
    site_list: list[dict],
) -> list[PointRow]:
    """provenance="native" rows carrying cloud_total AND cloud_low/mid/high
    together -- all four fields are genuinely native AIFS output, so (unlike
    ecmwf_hres) one row per (site, valid time, member) fully describes them."""
    if not cloud_path.exists():
        log.warning(
            "%s %s: cloud file missing, skipping: %s", model_name, run_init.isoformat(), cloud_path
        )
        return []
    fetched_at = file_fetched_at(cloud_path)

    per_member: dict[int, dict[str, xr.DataArray]] = {}
    for shortname in [total_shortname, *level_shortnames]:
        for member, da in _iter_members(cloud_path, shortname):
            per_member.setdefault(member, {})[shortname] = da

    expected_vars = {total_shortname, *level_shortnames}
    rows: list[PointRow] = []
    for member, das in sorted(per_member.items()):
        missing = expected_vars - das.keys()
        if missing:
            log.warning(
                "%s %s member %s: missing var(s) %s in %s, skipping this member",
                model_name,
                run_init.isoformat(),
                member,
                sorted(missing),
                cloud_path,
            )
            continue
        for site in site_list:
            band_values: dict[str, float | None] = {}
            for shortname in level_shortnames:
                v = _value_at(das[shortname], site["lat"], site["lon"])
                band_values[_SHORTNAME_TO_BAND[shortname]] = None if v is None else v * level_scale
            total_v = _value_at(das[total_shortname], site["lat"], site["lon"])
            total = None if total_v is None else total_v * total_scale
            for valid in valid_times:
                rows.append(
                    PointRow(
                        model=model_name,
                        run_init=run_init,
                        member=member,
                        site=site["name"],
                        valid=valid,
                        cloud_low=band_values.get("low"),
                        cloud_mid=band_values.get("mid"),
                        cloud_high=band_values.get("high"),
                        cloud_total=total,
                        provenance="native",
                        fetched_at=fetched_at,
                    )
                )
    return rows


def _extract_aifs(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    out_dir = raw_output_dir(model_name, run_init)
    by_step = _valid_times_by_step(model_config, run_init)
    total_shortname = model_config["cloud"]["total"]["param"]
    total_scale = _percent_scale(model_config["cloud"]["total"], "total")
    level_cfg = model_config["cloud"]["levels"]
    level_scale = _percent_scale(level_cfg, "levels")
    level_shortnames = list(level_cfg["params"])
    unknown = [s for s in level_shortnames if s not in _SHORTNAME_TO_BAND]
    if unknown:
        raise ValueError(
            f"{model_name}: unrecognized cloud.levels.params shortName(s) {unknown}, "
            f"expected a subset of {sorted(_SHORTNAME_TO_BAND)}"
        )

    site_list = all_sample_points()
    rows: list[PointRow] = []
    for step, valid_times in by_step.items():
        cloud_path = out_dir / f"cloud_f{step:03d}.grib2"
        rows.extend(
            _aifs_rows(
                model_name,
                run_init,
                cloud_path,
                total_shortname,
                total_scale,
                level_shortnames,
                level_scale,
                valid_times,
                site_list,
            )
        )
    return rows


_MODEL_BUILDERS = {
    "ecmwf_hres": _extract_total_only,
    "ecmwf_ens": _extract_total_only,
    "aifs_single": _extract_aifs,
    "aifs_ens": _extract_aifs,
}


@register("ecmwf-opendata")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Entry point per src/extract/registry.py's contract. Dispatches to the
    right builder for whichever of the four ecmwf-opendata models
    `model_name` names -- see module docstring for per-model row shape,
    provenance, and member conventions."""
    builder = _MODEL_BUILDERS.get(model_name)
    if builder is None:
        raise ValueError(
            f"ecmwf_extractor has no extract builder for model '{model_name}' "
            f"(known: {sorted(_MODEL_BUILDERS)})"
        )
    return builder(model_name, model_config, run_init)
