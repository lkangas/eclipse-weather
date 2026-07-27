"""The shared map renderer: render_frame() for one (model, run_init, step,
field) frame, render_run() for a whole archived run.

Not tied to any one tool, despite its history - this began as
tool1_renderer.py, when each tool's manifest script did its own rendering.
Rendering is now decoupled from the tools entirely: scripts/render_backfill.py
drives this module over every archived run, and Tool 1/2/3's manifest scripts
only describe the frames it has already written. That separation is what lets
production fetch -> render everything -> DELETE the raw GRIB (see CLAUDE.md's
disk-footprint note); a manifest script that still needed raw data would break
the moment raw data is deleted.

Unlike cloud_field_comparison.py (T31c), which always renders every
registered model's LATEST run for a fixed eclipse valid time,
this module renders one arbitrary (model, run_init, step, field) frame at a
time from data/raw/ - the full, un-cropped forecast range every fetcher now
fetches (see TASKS.md's 2026-07-23 archiver-consolidation note; there used
to be a separate raw_latest/ tree for this, retired the same day and
merged back into data/raw/).

Reuses the same private grid-opening helpers already built and verified in
src/extract/*.py, same reuse rationale as cloud_field_comparison.py: no
per-format GRIB parsing is duplicated here.

Covers gfs, arome_france, gefs_extended, arpege_europe, ecmwf_hres,
ecmwf_ens, aifs_single, aifs_ens, icon_eu, icon_global and aemet_harmonie -
every model in models.yaml with a spatial grid. Only the Open-Meteo point-API
models are outside it (ukmo_global, gem_global, jma_gsm, cma_grapes_global -
no grid to render).

aemet_harmonie is the odd one: its archive is AEMET's own rendered, colour-
mapped web-map layer rather than numeric output, so its reader inverts the
ESCALA legend instead of reading a field. That makes it materially coarser
than everything else here (9 bins of ~10 percentage points, total_only) - see
_aemet_harmonie_field and src/extract/aemet_extractor.py's provenance warning
before treating its values as comparable.

See _MODEL_READERS below; extend it the same way cloud_field_comparison.py's
own reader dict was built.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cfgrib
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

from src.config import DATA_RAW, DATA_ROOT, eclipse_config, get_model
from src.extract.ecmwf_extractor import _iter_members, _percent_scale
from src.extract.grib_regular_extractor import _gefs_levels_datasets, _gfs_layer_datasets
from src.extract.icon_extractor import (
    _ensure_remap_weights,
    _open_param_dataarray,
    _remap_icon_global_to_iberia,
)
from src.extract.icon_extractor import (
    _expected_filename as _icon_filename,
)
from src.extract.meteofrance_extractor import _cloud_dataset, _group_files, _step_hour_index
from src.fetchers.base import format_init_dir, full_range_steps
from src.viz.basemap import draw_basemap
from src.viz.cloud_field_comparison import TOTALITY_PATH_JSON, _crop

log = logging.getLogger(__name__)

OUTPUT_DIR = DATA_ROOT / "viz" / "tool1_frames"

with open(TOTALITY_PATH_JSON, encoding="utf-8") as _f:
    _TOTALITY_PATH = json.load(_f)
_TOTALITY_BAND_LON = [p["lon"] for p in _TOTALITY_PATH["northLimit"]] + [
    p["lon"] for p in reversed(_TOTALITY_PATH["southLimit"])
]
_TOTALITY_BAND_LAT = [p["lat"] for p in _TOTALITY_PATH["northLimit"]] + [
    p["lat"] for p in reversed(_TOTALITY_PATH["southLimit"])
]
_TOTALITY_CENTER_LON = [p["lon"] for p in _TOTALITY_PATH["centralLine"]]
_TOTALITY_CENTER_LAT = [p["lat"] for p in _TOTALITY_PATH["centralLine"]]

# Shared by arome_france AND arpege_europe - both are Meteo-France SP2-package
# group files with the identical lcc/mcc/hcc param names (T31c's own
# _field_arpege reuses the exact same mapping for this reason).
_AROME_VAR_BY_FIELD = {"low": "lcc", "mid": "mcc", "high": "hcc"}


def _gfs_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    path = DATA_RAW / "gfs" / format_init_dir(run_init) / f"f{step:03d}_cloud.grib2"
    if not path.exists():
        return None
    layers = _gfs_layer_datasets(path)
    ds = layers.get(field)
    if ds is None:
        return None
    var = next(iter(ds.data_vars))
    lons = ds.longitude.values.copy()
    lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    lats, lons_sorted, values = _crop(
        ds.latitude.values, lons[order], ds[var].values[:, order], bbox
    )
    return lats, lons_sorted, values


def _arome_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    if field == "total":
        return None  # SP2 has no native total field (meteofrance_extractor.py's own note)
    if field.startswith("prob_"):
        return None  # deterministic, single member - no ensemble spread to compute a P() from
    var = _AROME_VAR_BY_FIELD[field]
    for path in _group_files("arome_france", run_init):
        ds = _cloud_dataset(path)
        if ds is None:
            continue
        idx = _step_hour_index(ds)
        if step in idx:
            at_step = ds.isel(step=idx[step])
            return _crop(
                at_step.latitude.values, at_step.longitude.values, at_step[var].values, bbox
            )
    return None


def _arpege_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Same package/shape as arome_france (SP2, no native total field) - see
    T31c's own _field_arpege for the same reasoning."""
    if field == "total":
        return None
    if field.startswith("prob_"):
        return None  # deterministic, single member - no ensemble spread to compute a P() from
    var = _AROME_VAR_BY_FIELD[field]
    for path in _group_files("arpege_europe", run_init):
        ds = _cloud_dataset(path)
        if ds is None:
            continue
        idx = _step_hour_index(ds)
        if step in idx:
            at_step = ds.isel(step=idx[step])
            return _crop(
                at_step.latitude.values, at_step.longitude.values, at_step[var].values, bbox
            )
    return None


def _gefs_extended_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """gefs_extended's fetch() only ever fetches the control
    member (c00) - see herbie_fetcher.py's _MODEL_SPECS - so there is no
    ensemble `number` dimension to select here, unlike ecmwf_ens/aifs_ens
    below; every dataset opened from these files carries a single scalar
    number=0. Longitude is 0-360 (NOAA global grid), same conversion as
    _gfs_field above."""
    base_dir = DATA_RAW / "gefs_extended" / format_init_dir(run_init)

    if field == "total":
        path = base_dir / f"f{step:03d}_c00_total.grib2"
        if not path.exists():
            return None
        dsets = cfgrib.open_datasets(str(path))
        if not dsets:
            return None
        ds = dsets[0]
        var = "tcc"
    else:
        path = base_dir / f"f{step:03d}_c00_levels.grib2"
        if not path.exists():
            return None
        layers = _gefs_levels_datasets(path)
        ds = layers.get(field)
        if ds is None:
            return None
        var = "tcc"

    lons = ds.longitude.values.copy()
    lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    lats, lons_sorted, values = _crop(
        ds.latitude.values, lons[order], ds[var].values[:, order], bbox
    )
    return lats, lons_sorted, values


def _read_ecmwf_grid(path: Path, shortname: str, scale: float, bbox: dict) -> tuple | None:
    """One 2D grid for a single GRIB shortName, cropped to bbox. Grid is
    already -180..180 (ecmwf_extractor.py's own docstring) - no wraparound
    conversion needed, unlike the NOAA grids above.

    For ensemble files (ecmwf_ens/aifs_ens) this averages ACROSS ALL members
    (the ensemble mean), not one arbitrary representative member - per
    explicit user direction 2026-07-23: "the ensemble mean can be the
    aifs_ens entry to whatever quantity is selected." Same convention across
    every model this function serves, deterministic ones included - a
    deterministic file (ecmwf_hres's total, aifs_single) has exactly one
    "member", so averaging across it is a no-op, not a special case."""
    if not path.exists():
        return None
    members = _iter_members(path, shortname)
    if not members:
        return None
    stacked = np.stack([da.values for _, da in members], axis=0)
    mean_values = stacked.mean(axis=0)
    _, da0 = members[0]
    return _crop(da0.latitude.values, da0.longitude.values, mean_values * scale, bbox)


_PROB_CLOUD_THRESHOLD_PCT = 10.0  # locked in after comparing 0-30% via the
                                   # threshold-sweep review tool (now removed - its job was
                                   # done once this value was picked). Deliberately DIFFERENT
                                   # from site_ranking.py's own CLEAR_THRESHOLD_PCT_DEFAULT
                                   # (20.0, T32) - the two are intentionally separate metrics,
                                   # not kept in sync: site_ranking.py's report answers "which
                                   # site is most likely clear" (P(cloud_low < 20%), the useful
                                   # framing for ranking viewing sites), while this map shows
                                   # the complementary P(cloud_low >= 10%) - cloud probability,
                                   # not clear probability, at its own separately-tuned
                                   # threshold, per user direction. Not imported directly from
                                   # site_ranking.py to avoid pulling its polars/matplotlib
                                   # report machinery into the render path for one constant.


def _read_ecmwf_grid_prob_cloud(
    path: Path, shortname: str, scale: float, bbox: dict
) -> tuple | None:
    """Like _read_ecmwf_grid, but instead of the ensemble MEAN, computes the
    fraction of members with cloud_low AT OR ABOVE _PROB_CLOUD_THRESHOLD_PCT
    at each grid cell (as a percent 0-100, same scale/rendering convention as
    every other field) - cloud probability, the complement-in-spirit of
    site_ranking.py's own pooled P(cloud_low < 20%) clear-probability point
    metric (T32) but at its own separately-tuned 10% threshold, just
    per-pixel instead of per-named-site. `scale` must be
    applied BEFORE thresholding (unlike the plain mean, which commutes with a
    linear scale applied after averaging) - comparing against a threshold is
    not a linear operation.

    Ensemble-only: a deterministic single-member file would degrade to a
    meaningless binary 0%/100% map per cell, so callers must not invoke this
    for deterministic models (see _aifs_field's own ensemble-kind check)."""
    if not path.exists():
        return None
    members = _iter_members(path, shortname)
    if not members:
        return None
    stacked = np.stack([da.values for _, da in members], axis=0) * scale
    prob_pct = (stacked >= _PROB_CLOUD_THRESHOLD_PCT).mean(axis=0) * 100.0
    _, da0 = members[0]
    return _crop(da0.latitude.values, da0.longitude.values, prob_pct, bbox)


def _ecmwf_hres_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Native total (tcc) only. HRES has no native low/mid/high split, and
    Tool 1 deliberately does NOT render the humidity-derived L/M/H estimate
    src/derive/humidity_to_cloud.py can produce for it (see
    ecmwf_extractor.py/cloud_field_comparison.py for where that derivation is
    still used) - a real check against HRES's own native total (random-
    overlap combination of the derived bands vs native tcc) showed only
    ~0.35 correlation, mean |diff| ~14pp, max ~97pp - the RHc thresholds
    were tuned on a single GFS calibration sample (T22), never validated
    against HRES itself, and are not trustworthy enough to show here.
    Same "total only" shape as _ecmwf_ens_field below."""
    model_config = get_model("ecmwf_hres")
    out_dir = DATA_RAW / "ecmwf_hres" / format_init_dir(run_init)

    if field != "total":
        return None
    scale = _percent_scale(model_config["cloud"]["total"], "total")
    shortname = model_config["cloud"]["total"]["param"]
    return _read_ecmwf_grid(out_dir / f"tcc_f{step:03d}.grib2", shortname, scale, bbox)


def _ecmwf_ens_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """No native L/M/H for classic ENS (models.yaml: levels absent_in_open_data
    - that split only exists in aifs_ens, a different product) - total only."""
    if field != "total":
        return None
    model_config = get_model("ecmwf_ens")
    out_dir = DATA_RAW / "ecmwf_ens" / format_init_dir(run_init)
    scale = _percent_scale(model_config["cloud"]["total"], "total")
    shortname = model_config["cloud"]["total"]["param"]
    return _read_ecmwf_grid(out_dir / f"tcc_f{step:03d}.grib2", shortname, scale, bbox)


_AIFS_SHORTNAME_BY_FIELD = {"low": "lcc", "mid": "mcc", "high": "hcc"}


def _aifs_field(
    model_name: str, field: str, run_init: datetime, step: int, bbox: dict
) -> tuple | None:
    """Shared by aifs_single/aifs_ens - both write one cloud_f{step}.grib2
    with tcc/lcc/mcc/hcc all genuinely native (ecmwf_extractor.py's
    _aifs_rows note), unlike hres's native-total/derived-levels split.

    field="prob_{total,low,mid,high}" is the exception to the plain
    total/low/mid/high set - each is P(that quantity >= threshold) computed
    across members (see _read_ecmwf_grid_prob_cloud), independently per
    quantity rather than always reusing "low" - generalized from an earlier
    low-only version per explicit user direction, matching the per-quantity
    probability grid already validated in the review tooling. Ensemble-only:
    a deterministic model (aifs_single) has no member spread to compute a
    P() from, so it's a meaningless binary 0%/100% map per cell, not a real
    probability - skip it there rather than render something misleading."""
    model_config = get_model(model_name)
    path = DATA_RAW / model_name / format_init_dir(run_init) / f"cloud_f{step:03d}.grib2"
    if field == "total":
        scale = _percent_scale(model_config["cloud"]["total"], "total")
        shortname = model_config["cloud"]["total"]["param"]
        return _read_ecmwf_grid(path, shortname, scale, bbox)
    if field.startswith("prob_"):
        if "ensemble" not in model_config["kind"]:
            return None  # deterministic, single member - no ensemble spread to compute a P() from
        quantity = field[len("prob_"):]
        if quantity == "total":
            scale = _percent_scale(model_config["cloud"]["total"], "total")
            shortname = model_config["cloud"]["total"]["param"]
        else:
            scale = _percent_scale(model_config["cloud"]["levels"], "levels")
            shortname = _AIFS_SHORTNAME_BY_FIELD[quantity]
        return _read_ecmwf_grid_prob_cloud(path, shortname, scale, bbox)
    scale = _percent_scale(model_config["cloud"]["levels"], "levels")
    shortname = _AIFS_SHORTNAME_BY_FIELD[field]
    return _read_ecmwf_grid(path, shortname, scale, bbox)


def _aifs_single_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    return _aifs_field("aifs_single", field, run_init, step, bbox)


def _aifs_ens_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    return _aifs_field("aifs_ens", field, run_init, step, bbox)


_ICON_PARAM_BY_FIELD = {"low": "CLCL", "mid": "CLCM", "high": "CLCH", "total": "CLCT"}


def _icon_path(model_name: str, run_init: datetime, step: int, param: str) -> Path:
    """The path dwd_bz2_fetcher.py's fetch() wrote for this (step, param),
    reconstructed from models.yaml's own url_template - same convention as
    icon_extractor.py's _expected_filename()."""
    model_config = get_model(model_name)
    url_template = model_config["source"]["url_template"]
    filename = _icon_filename(
        url_template, hh=run_init.strftime("%H"), yyyymmddhh=run_init.strftime("%Y%m%d%H"),
        fff=f"{step:03d}", param=param,
    )
    return DATA_RAW / model_name / format_init_dir(run_init) / filename


def _icon_eu_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Already regular lat/lon - direct read, no remap (unlike icon_global
    below)."""
    if field.startswith("prob_"):
        return None  # deterministic, single member - no ensemble spread to compute a P() from
    param = _ICON_PARAM_BY_FIELD[field]
    path = _icon_path("icon_eu", run_init, step, param)
    if not path.exists():
        return None
    da = _open_param_dataarray(path, param)
    if da is None:
        return None
    return _crop(da.latitude.values, da.longitude.values, da.values, bbox)


def _icon_global_field(field: str, run_init: datetime, step: int, bbox: dict) -> tuple | None:
    """Native icosahedral grid - reuses icon_extractor.py's cached cdo remap
    weights (already built/verified for the eclipse archiver's own
    DATA_RAW-rooted icon_global path) to remap+crop to Iberia in one call,
    same as cloud_field_comparison.py's _field_icon_global."""
    if field.startswith("prob_"):
        return None  # deterministic, single member - no ensemble spread to compute a P() from
    param = _ICON_PARAM_BY_FIELD[field]
    src_path = _icon_path("icon_global", run_init, step, param)
    if not src_path.exists():
        return None
    grid_path, weights_path = _ensure_remap_weights()
    with tempfile.TemporaryDirectory(prefix="tool1_icon_global_remap_") as tmp:
        remapped = _remap_icon_global_to_iberia(src_path, bbox, grid_path, weights_path, Path(tmp))
        da = _open_param_dataarray(remapped, param)
        if da is None:
            return None
        da = da.load()  # must load into memory before the temp dir is cleaned up
    # Already cropped by -sellonlatbox during the remap; no further crop needed.
    return da.latitude.values, da.longitude.values, da.values



# AEMET's legend starts at 10%; below it the product draws nothing, so a
# transparent pixel means "<10% cloud" - or "outside the domain", which this
# product gives no way to distinguish.
#
# 0.0, not the 5.0 bin-midpoint the other bins use. 5.0 is defensible as a
# number but wrong as a pixel: render_frame applies PowerNorm(gamma=0.4) to
# make thin cloud visible, which maps 5% to 0.30 of the Blues ramp, and since
# ~97% of a typical raster is transparent the whole map came out a flat light
# blue - implying widespread thin cloud AEMET never forecast. 0.0 asserts "0%"
# where AEMET only says "<10%", understating by at most 10 points in the one
# region where that distinction matters least.
_AEMET_UNDRAWN_VALUE = 0.0
_AEMET_ALPHA_OPAQUE = 128


def _aemet_harmonie_field(field: str, run_init: datetime, step: int, bbox: dict):
    """aemet_harmonie is not scientific data: the archived GeoTIFFs are
    AEMET's own RENDERED, COLOUR-MAPPED web-map layer (4-band RGBA), with no
    numeric cloud band to read. Values are recovered by inverting the ramp
    through the file's own ESCALA legend tag - the same _parse_escala +
    nearest-RGB-stop + bin-midpoint path src/extract/aemet_extractor.py uses
    per site, vectorised here because a map needs ~256,000 of those lookups
    rather than 29.

    Nearest-match rather than exact lookup because the rasters are lossily
    compressed: one real file held 12,672 distinct opaque colours derived from
    a 9-colour legend.

    Inherently coarse - 10-point legend bins on top of total_only provenance.
    See the extractor's module docstring for the full provenance warning; do
    not read a value here as comparable in precision to a native GRIB field.
    """
    if field != "total":
        return None   # T07(b): AEMET publishes no L/M/H anywhere

    import rasterio

    from src.extract.aemet_extractor import _parse_escala

    valid = run_init + timedelta(hours=step)
    path = (DATA_RAW / "aemet_harmonie" / format_init_dir(run_init)
            / f"aemet_harmonie_nubosidad_{valid:%Y%m%dT%H%M%S}Z.tif")
    if not path.exists():
        return None

    with rasterio.open(path) as ds:
        stops = _parse_escala(ds.tags())
        bands = ds.read()
        transform, height, width = ds.transform, ds.height, ds.width

    rgb = bands[:3].astype(np.int16).transpose(1, 2, 0)
    alpha = bands[3] if bands.shape[0] >= 4 else np.full((height, width), 255)
    stop_rgb = np.array([s[2] for s in stops], np.int16)
    mids = np.array([(lo + hi) / 2.0 for lo, hi, _ in stops], np.float32)

    d2 = ((rgb[:, :, None, :] - stop_rgb[None, None, :, :]) ** 2).sum(-1)
    values = mids[d2.argmin(-1)]
    values = np.where(alpha < _AEMET_ALPHA_OPAQUE, _AEMET_UNDRAWN_VALUE, values).astype(np.float32)

    lons_1d = transform.c + (np.arange(width) + 0.5) * transform.a
    lats_1d = transform.f + (np.arange(height) + 0.5) * transform.e
    col = (lons_1d >= bbox["lon_min"]) & (lons_1d <= bbox["lon_max"])
    row = (lats_1d >= bbox["lat_min"]) & (lats_1d <= bbox["lat_max"])
    if not col.any() or not row.any():
        return None
    lons, lats = np.meshgrid(lons_1d[col], lats_1d[row])
    return lats, lons, values[np.ix_(row, col)]


_MODEL_READERS = {
    "gfs": _gfs_field,
    "arome_france": _arome_field,
    "arpege_europe": _arpege_field,
    "gefs_extended": _gefs_extended_field,
    "ecmwf_hres": _ecmwf_hres_field,
    "ecmwf_ens": _ecmwf_ens_field,
    "aifs_single": _aifs_single_field,
    "aifs_ens": _aifs_ens_field,
    "icon_eu": _icon_eu_field,
    "icon_global": _icon_global_field,
    "aemet_harmonie": _aemet_harmonie_field,
}


# The only readers with real per-member probability support - see
# _aifs_field's own prob_ branch (further gated internally on "ensemble" in
# kind, to separate aifs_ens from aifs_single). gefs_extended's models.yaml
# kind: ensemble is true too (the GEFS product family genuinely has 31
# members upstream), but this project's own fetcher only ever pulls the
# control member c00 - see _gefs_extended_field's own docstring - so
# per-pixel probability across "members" would be meaningless there; its
# reader never implements a prob_ branch at all regardless of what the
# YAML kind label says about the upstream product. Trust the reader
# (what this project's own code actually supports), not the label.
_PROB_CAPABLE_READERS = {_aifs_single_field, _aifs_ens_field}


def supported_fields(model_id: str) -> list[str]:
    """Which of the 3 canonical fields (total, hml_composite,
    prob_hml_composite) this model can actually produce - derived from
    models.yaml/the reader registry, never a separately-maintained table
    (CLAUDE.md's own single-source-of-truth rule), so callers never attempt
    a render that's known in advance to be structurally impossible for this
    model (e.g. hml_composite for ecmwf_hres/ecmwf_ens, prob_hml_composite
    for every model but aifs_ens, total for arome_france/arpege_europe).

    cloud.levels must be status == "confirmed" (genuinely native low/mid/
    high) to count, not merely present - ecmwf_hres's levels are status:
    derived (the humidity-derived estimate this renderer deliberately never
    uses, see _ecmwf_hres_field's own docstring) and ecmwf_ens's are
    status: absent_in_open_data; neither is real hml_composite material
    despite the key existing in models.yaml for ecmwf_hres.

    It must ALSO not be present: false. "confirmed" is about the metadata
    having been verified, and an entry can perfectly well confirm that a
    field does NOT exist - aemet_harmonie's levels entry is exactly that,
    T07(b)'s adversarially double-checked finding that AEMET publishes no
    L/M/H at all."""
    model_config = get_model(model_id)
    cloud = model_config.get("cloud", {})
    levels = cloud.get("levels", {})
    # `status: confirmed` means "this metadata was verified", NOT "this field
    # exists" - and for aemet_harmonie what T07(b) verified is that AEMET has
    # no low/mid/high anywhere, recorded as present: false. Reading status
    # alone gave that model hml_composite, a composite of three fields it can
    # never supply, instead of the single blended total it actually publishes.
    has_native_levels = levels.get("status") == "confirmed" and levels.get("present", True)

    # total is a FALLBACK, not a parallel quantity: a model with native
    # low/mid/high shows the H/M/L composite, and total is only rendered for
    # the models that have no levels to composite (ecmwf_hres, ecmwf_ens).
    # Rendering both for a model that has levels produces frames no tool ever
    # displays - the composite always wins - so it is pure waste.
    if not has_native_levels:
        return ["total"] if "total" in cloud else []

    fields = ["hml_composite"]
    if "ensemble" in model_config["kind"] and _MODEL_READERS[model_id] in _PROB_CAPABLE_READERS:
        fields.append("prob_hml_composite")
    return fields


def _steps_for_run(model_id: str, model_config: dict, run_init: datetime) -> list[int]:
    """Which steps to attempt for this run.

    Normally models.yaml's declared schedule. aemet_harmonie is the exception:
    its files are named by VALID TIME rather than step, and its real run length
    varies - 48 hourly files on most runs but 90 on 2026-07-25 00Z, against a
    declared length of 48 h. Config would therefore both miss real hours and
    invent steps that were never published, so for this model the files on
    disk ARE the schedule.
    """
    if model_id != "aemet_harmonie":
        return full_range_steps(model_config, run_init)

    run_dir = DATA_RAW / model_id / format_init_dir(run_init)
    if not run_dir.is_dir():
        return []
    steps = []
    for path in run_dir.glob(f"{model_id}_nubosidad_*.tif"):
        stamp = path.stem.rsplit("_", 1)[-1]
        try:
            valid = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue
        delta = (valid - run_init).total_seconds() / 3600
        if delta >= 0 and float(delta).is_integer():
            steps.append(int(delta))
    return sorted(steps)


def render_run(model_id: str, run_init: datetime) -> dict[int, dict[str, bool]]:
    """Render every step x every structurally-supported field (see
    supported_fields()) for one already-fetched (model_id, run_init) - the
    single reusable rendering entry point, called identically by:
      - a desktop backfill loop, once per already-archived run found on
        disk (newest-first - see scripts/generate_tool{2,3}_manifest.py's
        own _archived_run_inits())
      - the future production fetch->render->delete pipeline, once right
        after a fresh fetch, before that run's raw data gets deleted (see
        CLAUDE.md's disk-footprint note - this is the render step that must
        happen before deletion, not lazily whenever some tool's manifest
        script next happens to run)

    Returns {step: {field: has_data}} so callers can build their own
    manifest.json (Tool 1/2/3 each want a different subset of runs/steps)
    without ever touching raw data themselves, and production can use it to
    confirm rendering actually produced something before deleting the
    source."""
    model_config = get_model(model_id)
    steps = _steps_for_run(model_id, model_config, run_init)
    fields = supported_fields(model_id)
    result: dict[int, dict[str, bool]] = {}
    for step in steps:
        result[step] = {}
        for field in fields:
            _, has_data = render_frame(model_id, run_init, step, field)
            result[step][field] = has_data
    return result


# Display labels for the title text - same small per-model mapping already
# duplicated across scripts/generate_tool{1,2,3}_manifest.py; kept local
# here too rather than importing a script module into src/.
_MODEL_LABELS = {
    "aemet_harmonie": "AEMET HARMONIE",
    "gfs": "GFS",
    "gefs_extended": "GEFS Extended",
    "arome_france": "AROME France",
    "arpege_europe": "ARPEGE Europe",
    "ecmwf_hres": "ECMWF HRES",
    "ecmwf_ens": "ECMWF ENS",
    "aifs_single": "AIFS Single",
    "aifs_ens": "AIFS ENS",
    "icon_eu": "ICON EU",
    "icon_global": "ICON Global",
}

# "temp" reserved for future surface-temperature frames (deferred - none of
# the fetchers pull it yet, see models.yaml's surface_temp entries/T37).
# Short by design - these go straight into every rendered PNG's title, so
# kept to a couple words (the RGB channel legend used to live here too;
# moved out per explicit user direction against title bloat).
_FIELD_LABELS = {
    "total": "Total",
    "low": "Low",
    "mid": "Mid",
    "high": "High",
    "prob_low": "Prob (Low)",
    "prob_mid": "Prob (Mid)",
    "prob_high": "Prob (High)",
    "hml_composite": "H/M/L",
    "prob_hml_composite": "Prob H/M/L",
    "temp": "Temp",
}

# Composite fields aren't a single reader call - render_frame() reads each
# of these three sub-fields via the model's own reader and alpha-composites
# them (R=high,G=mid,B=low), instead of pcolormesh-ing one scalar array.
_COMPOSITE_SUBFIELDS = {
    "hml_composite": ("high", "mid", "low"),
    "prob_hml_composite": ("prob_high", "prob_mid", "prob_low"),
}

# Cloud fields (0-100% cloud fraction) wash low values out under linear
# scaling - gamma=0.4 stretch fixes that (picked after comparing 0.25/0.4/0.6
# against real data this session). Probability fields get a milder gamma=0.6
# (locked in separately, see TASKS.md). Applies both to the scalar
# pcolormesh path below and to each channel of the two composite fields.
_CLOUD_GAMMA = 0.40
_PROB_GAMMA = 0.60


def _gamma_for_field(field: str) -> float:
    return _PROB_GAMMA if field.startswith("prob_") else _CLOUD_GAMMA


def _fmt_dm_z(dt: datetime) -> str:
    """'10.8. 00Z' style - day.month. (no year, no leading zeros) + hour Z."""
    return f"{dt.day}.{dt.month}. {dt:%H}Z"


_MAP_ASPECT = 1.3  # must match the ax.set_aspect() call below
_FIG_WIDTH_IN = 6.0
_TITLE_HEIGHT_IN = 0.35  # just enough for one line of 10pt title text


def _figure_layout(bbox: dict) -> tuple[float, float, float]:
    """Figure (width, height) sized so the map fills it edge-to-edge (no
    margins) with only a thin strip reserved on top for the title, plus the
    axes-box top fraction to pass to subplots_adjust(). Height is derived
    from the bbox's lon/lat span and _MAP_ASPECT so the map area itself is
    never letterboxed - if we instead used a fixed figsize and relied on
    set_aspect's default 'box' adjustment, matplotlib would shrink the axes
    box to fit the aspect and leave white bars, exactly what this avoids."""
    lon_span = bbox["lon_max"] - bbox["lon_min"]
    lat_span = bbox["lat_max"] - bbox["lat_min"]
    map_height_in = _FIG_WIDTH_IN * (lat_span * _MAP_ASPECT) / lon_span
    fig_height_in = map_height_in + _TITLE_HEIGHT_IN
    return _FIG_WIDTH_IN, fig_height_in, map_height_in / fig_height_in


def render_frame(
    model_name: str, run_init: datetime, step: int, field: str, output_path: Path | None = None
) -> tuple[Path, bool]:
    """Render one (model, run_init, step, field) map to a PNG. Returns
    (path, has_data) - has_data is False when this specific field has no
    native data for this model (e.g. arome_france's "total") OR when this
    step isn't actually published at all (e.g. arome_france's group files
    start at +1h, not +0h, despite full_range_steps() assuming every model
    publishes a step-0 field - see TASKS.md T34 for the real case that
    surfaced this). Callers use has_data to decide whether a step is worth
    listing at all. NO file is written when there is no data: a placeholder
    PNG would be indistinguishable from a real map to the manifest scripts,
    which see only file existence now that they no longer read raw data, and
    no tool ever displayed one anyway - all three gate on has_data and show
    their own prose instead. So "a frame exists on disk" == "has real data".

    field in _COMPOSITE_SUBFIELDS (hml_composite/prob_hml_composite) is
    dispatched to _render_composite_frame instead - it needs three reads
    and an RGB composite, not one scalar pcolormesh."""
    if model_name not in _MODEL_READERS:
        raise KeyError(f"frame_renderer has no reader for model '{model_name}'")

    output_path = output_path or (
        OUTPUT_DIR / model_name / field / f"{format_init_dir(run_init)}_{step:03d}.png"
    )

    bbox = eclipse_config()["bbox"]

    if field in _COMPOSITE_SUBFIELDS:
        return _render_composite_frame(model_name, run_init, step, field, bbox, output_path)

    if output_path.exists():
        # Already rendered, and that is the whole answer: a frame is only ever
        # written when the reader returned real data (see the no-placeholder
        # branch below), so its existence IS has_data=True. Returning here
        # WITHOUT calling the reader is what makes re-walking an already
        # rendered archive cheap - opening the GRIB just to recompute a flag
        # the file's own existence already proves cost ~10 minutes per
        # already-done run in the render worker's first sweep, which read as
        # the worker being stalled. Archived data for a past step never
        # changes, so there is nothing to re-derive.
        return output_path, True

    try:
        result = _MODEL_READERS[model_name](field, run_init, step, bbox)
    except Exception:
        log.exception("frame_renderer: %s/%s/+%dh/%s failed", model_name, run_init, step, field)
        result = None

    if result is None:
        # No file at all when there's no data - see the module note on why
        # "a frame exists on disk" is the has_data signal now. A placeholder
        # PNG here would be indistinguishable from a real map to the
        # manifest scripts, which only see file existence (they no longer
        # read raw data), and no tool ever displayed it anyway: all three
        # gate on has_data and render their own prose instead.
        return output_path, False

    lats, lons, values = result
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    norm = mcolors.PowerNorm(gamma=_gamma_for_field(field), vmin=0, vmax=100)
    ax.pcolormesh(
        lons, lats, values, cmap="Blues", norm=norm,
        shading="auto", rasterized=True,
    )
    # Coastline/roads/eclipse-path drawn stroke-only, on top of the
    # cloud fill - see basemap.py's docstring for why (no fill: the
    # pcolormesh above already covers the whole bbox, land included).
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "r-", linewidth=0.8, alpha=0.6, zorder=7)
    ax.plot(
        _TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "r--", linewidth=1, alpha=0.8, zorder=7
    )

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    label = _MODEL_LABELS.get(model_name, model_name)
    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"{label} · {_fmt_dm_z(run_init)} → {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=10,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=100)
    plt.close(fig)
    return output_path, result is not None


def _render_composite_frame(
    model_name: str, run_init: datetime, step: int, field: str, bbox: dict, output_path: Path
) -> tuple[Path, bool]:
    """R=high,G=mid,B=low alpha-composite over a white background - see
    _COMPOSITE_SUBFIELDS for which three sub-fields feed which composite.
    Each channel gets the same gamma stretch its own scalar field would
    (cloud fields 0.4, prob fields 0.6) before compositing, so a thin/faint
    layer still shows up as a tint instead of vanishing into white. High
    composited first (as if farthest away), then mid, then low last/on top
    - low cloud (or P(low)) is the most decisive layer for whether the
    eclipse is actually visible, so it should visually win where layers
    overlap."""
    if output_path.exists():
        # Same reasoning as render_frame()'s own early return, and it matters
        # more here: a composite needs THREE reads per frame, so recomputing
        # has_data for an already-drawn frame was the single most expensive
        # pointless operation in the codebase. The frame's existence already
        # proves all three sub-fields had data.
        return output_path, True

    high_field, mid_field, low_field = _COMPOSITE_SUBFIELDS[field]
    try:
        high_result = _MODEL_READERS[model_name](high_field, run_init, step, bbox)
        mid_result = _MODEL_READERS[model_name](mid_field, run_init, step, bbox)
        low_result = _MODEL_READERS[model_name](low_field, run_init, step, bbox)
    except Exception:
        log.exception(
            "frame_renderer: %s/%s/+%dh/%s (composite) failed", model_name, run_init, step, field
        )
        high_result = mid_result = low_result = None

    has_data = high_result is not None and mid_result is not None and low_result is not None

    if not has_data:
        # No file at all - same reasoning as render_frame()'s own early
        # return: a placeholder is indistinguishable from a real map to a
        # manifest script that only sees file existence.
        return output_path, False

    gamma = _gamma_for_field(field)
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    lats, lons, hval = high_result
    _, _, mval = mid_result
    _, _, lval = low_result

    r_alpha = np.clip(hval / 100, 0, 1) ** gamma
    g_alpha = np.clip(mval / 100, 0, 1) ** gamma
    b_alpha = np.clip(lval / 100, 0, 1) ** gamma

    canvas = np.ones(r_alpha.shape + (3,))
    for alpha, color in (
        (r_alpha, np.array([1.0, 0.0, 0.0])),   # high -> red
        (g_alpha, np.array([0.0, 0.65, 0.0])),  # mid  -> green
        (b_alpha, np.array([0.0, 0.3, 1.0])),   # low  -> blue
    ):
        canvas = canvas * (1 - alpha[..., None]) + color * alpha[..., None]

    # imshow needs ascending lat order with origin="lower" to place
    # north at the top - flip if the source grid is north-to-south.
    if lats[0] > lats[-1]:
        lats = lats[::-1]
        canvas = canvas[::-1, :, :]

    ax.imshow(
        canvas,
        extent=(bbox["lon_min"], bbox["lon_max"], bbox["lat_min"], bbox["lat_max"]),
        origin="lower",
        aspect="auto",
        interpolation="nearest",
    )
    draw_basemap(ax, bbox)
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "k-", linewidth=0.8, alpha=0.5, zorder=7)
    ax.plot(
        _TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "k--", linewidth=1, alpha=0.7, zorder=7
    )

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    label = _MODEL_LABELS.get(model_name, model_name)
    valid = run_init + timedelta(hours=step)
    ax.set_title(
        f"{label} · {_fmt_dm_z(run_init)} → {_fmt_dm_z(valid)} (+{step}h)",
        fontsize=10,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=100)
    plt.close(fig)
    return output_path, has_data
