"""One 2 m-temperature panel per model, all at ONE valid time, all on ONE
fixed colour scale - the settled panel design applied to real data.

This replaces the earlier candidate-gallery experiment
(scripts/render_temp_panel_experiment.py, deleted 2026-07-27). The design
questions it asked are answered; nothing here is a variant, a mode or an
alternative. Exactly one rendering style is applied uniformly, so the only
thing that differs between panels is the model.

THE SETTLED DESIGN
------------------
  * Absolute 2 m temperature in Celsius. No anomaly, no difference.
  * RdYlBu_r, FIXED 0-44 C, 2 C bands, extend="both", plain Normalize.
    (Not PowerNorm - a gamma stretch saturates the whole peninsula.)
  * Thin band edges on every 2 C boundary (0.25 pt, "0.25" grey).
  * Heavier black isotherms at 20/30/40 C as reading anchors.
  * Roads "F11": Major Highway "0.4" grey, 0.575 pt, alpha 1.0, zorder 6;
    Secondary Highway left at today's draw_basemap() defaults ("0.4",
    0.3 pt, alpha 0.35, zorder 5).
  * Totality overlay "C2": plain black, no white halo.
  * Coastline unchanged from production: black, 0.5 pt.
  * Compact horizontal colourbar inset bottom-left, ticks every 5 C.
  * No clabel-labelled isotherms (unreadable at production dpi).
  * frame_renderer.py's own _figure_layout() and dpi=100, so every panel is
    pixel-comparable to a production frame. frame_renderer.py itself is NOT
    modified and none of its geometry is re-derived here.

WHICH MODELS
------------
Read from config/models.yaml, never hardcoded: every model whose
`surface_temp.height` is `2m`. Today that is eight gridded models - gfs,
gefs_extended, ecmwf_hres, ecmwf_ens, aifs_single, aifs_ens, icon_eu,
icon_global.

Deliberately absent:
  * arome_france / arpege_europe - their SP2 `t` is `height: skin`, i.e.
    SKIN temperature (verified T45, 2026-07-27), with roughly double the
    diurnal amplitude. It does not belong on a shared 2 m scale.
  * the Open-Meteo point models (ukmo_global, gem_global, jma_gsm,
    cma_grapes_global) and aemet_harmonie - no gridded reader exists.

DATA
----
Temperature is not fetched by the archiver yet, so this script fetches it
itself, ONE STEP per model (the ensembles are ~33 MB for that one step;
a range would be rude and pointless).

  gfs, gefs_extended   herbie idx byte-range, ":TMP:2 m above ground:"
  ecmwf_hres/ens,
  aifs_single/ens      ecmwf-opendata, param "2t" on the REQUEST side -
                       cfgrib then names the decoded variable "t2m".
                       models.yaml keeps both (`param:` / `cfgrib_var:`).
  icon_eu              opendata.dwd.de T_2M, regular lat/lon, direct read
  icon_global          opendata.dwd.de T_2M, icosahedral -> cdo remap via
                       icon_extractor.py's cached DWD weight bundle

Ensembles (ecmwf_ens, aifs_ens) are rendered as the ENSEMBLE MEAN across
members, matching frame_renderer._read_ecmwf_grid's convention for every
other quantity. All models are Kelvin -> Celsius.

Usage (inside the Docker container - GRIB deps and cdo required):
    docker cp scripts/render_temp_panels.py eclipse-scheduler:/app/scripts/
    docker exec eclipse-scheduler /app/.venv/bin/python -m scripts.render_temp_panels
"""

from __future__ import annotations

import bz2
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cfgrib
import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from herbie import Herbie
from matplotlib.patches import Rectangle

from src.config import DATA_ROOT, eclipse_config, load_models
from src.extract.icon_extractor import (
    _ensure_remap_weights,
    _open_param_dataarray,
    _remap_icon_global_to_iberia,
)
from src.viz.basemap import _clip, _load_land, _load_roads
from src.viz.cloud_field_comparison import _crop
from src.viz.frame_renderer import (
    OUTPUT_DIR,
    _TOTALITY_BAND_LAT,
    _TOTALITY_BAND_LON,
    _TOTALITY_CENTER_LAT,
    _TOTALITY_CENTER_LON,
    _figure_layout,
    _fmt_dm_z,
)

log = logging.getLogger(__name__)

PANEL_DIR = OUTPUT_DIR / "temp_panels"
GRID_JSON = OUTPUT_DIR / "temp_panel_grid.json"
CACHE = DATA_ROOT / "cache" / "temp_panels"

# ONE run, ONE step, every model - comparability is the whole point of the
# page, so a model that cannot reach this valid time renders NOTHING rather
# than silently substituting a different hour.
RUN_INIT = datetime(int(os.environ.get("TEMP_PANEL_YEAR", 2026)),
                    int(os.environ.get("TEMP_PANEL_MONTH", 7)),
                    int(os.environ.get("TEMP_PANEL_DAY", 27)),
                    int(os.environ.get("TEMP_PANEL_CYCLE", 0)))
STEP = int(os.environ.get("TEMP_PANEL_STEP", 18))
VALID = RUN_INIT + timedelta(hours=STEP)

_DWD_ROOT = "https://opendata.dwd.de/weather/nwp"
_UA = "eclipse-weather-archiver/0.1 (contact: lauri@farsight.space)"
KELVIN = 273.15


# ---------------------------------------------------------------------------
# Data access - every reader returns (lats, lons, values in degrees Celsius)
# ---------------------------------------------------------------------------


def _to_iberia(ds_or_da, var: str | None, bbox: dict) -> tuple:
    """Crop to the Iberia bbox, converting a 0-360 grid to -180..180 first.
    ICON and the ECMWF grids are already -180..180 (checked live); the NOAA
    grids are not."""
    da = ds_or_da if var is None else ds_or_da[var]
    lons = da.longitude.values.copy()
    if lons.max() > 180:
        lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    return _crop(da.latitude.values, lons[order], da.values[:, order], bbox)


def _herbie_2m(herbie_model: str, product: str, member: str | None, bbox: dict) -> tuple:
    """GFS/GEFS: one clean instantaneous TMP message per step - unlike
    cloud/rain, 2 m TMP has no windowed-average twin to exclude
    (models.yaml's own T37 note)."""
    kwargs = dict(model=herbie_model, product=product, fxx=STEP,
                  save_dir=str(CACHE), verbose=False)
    if member is not None:
        kwargs["member"] = member
    h = Herbie(RUN_INIT, **kwargs)
    path = h.download(search=r":TMP:2 m above ground:", verbose=False, errors="raise")
    ds = cfgrib.open_datasets(str(path))[0]
    lats, lons, values = _to_iberia(ds, "t2m", bbox)
    return lats, lons, values - KELVIN


def read_gfs(bbox: dict) -> tuple:
    return _herbie_2m("gfs", "pgrb2.0p25", None, bbox)


def read_gefs_extended(bbox: dict) -> tuple:
    # Control member only, same as herbie_fetcher.py's _MODEL_SPECS: this
    # project never pulls GEFS's perturbed members, so there is no ensemble
    # mean available here even though the upstream product has 31 members.
    return _herbie_2m("gefs", "atmos.5", "c00", bbox)


def _iter_2t_members(path: Path) -> list[tuple[int, xr.DataArray]]:
    """Every (member, 2-D DataArray) pair for 2 m temperature in `path`.

    Same member-splitting logic as ecmwf_extractor._iter_members (cfgrib puts
    a control member with a scalar `number` coord in a different hypercube
    from the perturbed members' `number` dimension), but that helper assumes
    the GRIB shortName and the decoded variable name are the same string,
    which holds for tcc/lcc/... and NOT for 2 m temperature: the filter-side
    name is `2t`, the decoded variable is `t2m` (models.yaml's param: /
    cfgrib_var: split, corrected T45). Hence a local variant rather than a
    reuse."""
    dsets = cfgrib.open_datasets(
        str(path), backend_kwargs={"filter_by_keys": {"shortName": "2t"}}
    )
    out: list[tuple[int, xr.DataArray]] = []
    for ds in dsets:
        da = ds["t2m"]
        if "number" in da.dims:
            out.extend((int(n), da.sel(number=int(n))) for n in da["number"].values)
        elif "number" in da.coords:
            out.append((int(da["number"].values), da))
        else:
            out.append((-1, da))
    return out


def _ecmwf_2m(model_id: str, request: dict, bbox: dict) -> tuple:
    """Any ecmwf-opendata product's 2t, averaged across whatever members the
    file contains - the ensemble mean for ecmwf_ens/aifs_ens, a no-op for the
    single-member deterministic files, exactly as
    frame_renderer._read_ecmwf_grid does it for cloud."""
    from ecmwf.opendata import Client

    dst = CACHE / f"{model_id}_{RUN_INIT:%Y%m%d%H}_2t_f{STEP:03d}.grib2"
    if not dst.exists() or dst.stat().st_size == 0:
        Client().retrieve(
            request={**request, "date": RUN_INIT.date(), "time": RUN_INIT.hour,
                     "step": STEP, "param": "2t"},
            target=str(dst),
        )
    members = _iter_2t_members(dst)
    if not members:
        raise RuntimeError(f"{model_id}: no 2t messages in {dst}")
    stacked = np.stack([da.values for _, da in members], axis=0)
    _, da0 = members[0]
    mean = xr.DataArray(stacked.mean(axis=0), coords=da0.coords, dims=da0.dims)
    lats, lons, values = _to_iberia(mean, None, bbox)
    return lats, lons, values - KELVIN, len(members)


def read_ecmwf_hres(bbox: dict) -> tuple:
    return _ecmwf_2m("ecmwf_hres", {"stream": "oper", "type": "fc"}, bbox)


def read_ecmwf_ens(bbox: dict) -> tuple:
    return _ecmwf_2m("ecmwf_ens", {"stream": "enfo", "type": ["cf", "pf"]}, bbox)


def read_aifs_single(bbox: dict) -> tuple:
    return _ecmwf_2m("aifs_single", {"model": "aifs-single"}, bbox)


def read_aifs_ens(bbox: dict) -> tuple:
    return _ecmwf_2m("aifs_ens", {"model": "aifs-ens"}, bbox)


def _dwd_t2m_file(model_id: str, url: str) -> Path:
    dst = CACHE / f"{model_id}_{RUN_INIT:%Y%m%d%H}_{STEP:03d}_T_2M.grib2"
    if not dst.exists() or dst.stat().st_size == 0:
        resp = httpx.get(url, timeout=180.0, follow_redirects=True, headers={"User-Agent": _UA})
        resp.raise_for_status()
        dst.write_bytes(bz2.decompress(resp.content))
    return dst


def read_icon_eu(bbox: dict) -> tuple:
    url = (f"{_DWD_ROOT}/icon-eu/grib/{RUN_INIT:%H}/t_2m/icon-eu_europe_regular-lat-lon_"
           f"single-level_{RUN_INIT:%Y%m%d%H}_{STEP:03d}_T_2M.grib2.bz2")
    ds = xr.open_dataset(str(_dwd_t2m_file("icon_eu", url)), engine="cfgrib")
    lats, lons, values = _to_iberia(ds, "t2m", bbox)
    return lats, lons, values - KELVIN


def read_icon_global(bbox: dict) -> tuple:
    """Native icosahedral - reuses icon_extractor.py's cached DWD cdo remap
    weight bundle to remap+crop to Iberia in one call, exactly as
    frame_renderer._icon_global_field does for cloud."""
    url = (f"{_DWD_ROOT}/icon/grib/{RUN_INIT:%H}/t_2m/icon_global_icosahedral_"
           f"single-level_{RUN_INIT:%Y%m%d%H}_{STEP:03d}_T_2M.grib2.bz2")
    src = _dwd_t2m_file("icon_global", url)
    grid_path, weights_path = _ensure_remap_weights()
    with tempfile.TemporaryDirectory(prefix="temp_panels_icon_global_") as tmp:
        remapped = _remap_icon_global_to_iberia(src, bbox, grid_path, weights_path, Path(tmp))
        da = _open_param_dataarray(remapped, "t2m")
        if da is None:
            raise RuntimeError("icon_global: remapped file had no readable 2 m field")
        da = da.load()  # must load before the temp dir is cleaned up
    # -sellonlatbox already cropped it; no further crop needed.
    return da.latitude.values, da.longitude.values, da.values - KELVIN


READERS = {
    "gfs": read_gfs,
    "gefs_extended": read_gefs_extended,
    "ecmwf_hres": read_ecmwf_hres,
    "ecmwf_ens": read_ecmwf_ens,
    "aifs_single": read_aifs_single,
    "aifs_ens": read_aifs_ens,
    "icon_eu": read_icon_eu,
    "icon_global": read_icon_global,
}


# ---------------------------------------------------------------------------
# The settled design - drawing primitives
# ---------------------------------------------------------------------------

FIELD_Z = 1

LEVELS = np.arange(0, 45, 2.0)       # 0..44 C in 2 C bands
TICKS = np.arange(0, 45, 5.0)        # colourbar ticks every 5 C
EMPHASIS_ISOTHERMS = [20, 30, 40]    # heavier black reading anchors
CMAP = "RdYlBu_r"

# "F11": major highways opaque and midway between today's 0.3 pt and a bold
# 0.85 pt, so they read as a continuous line at dpi=100 (one point is
# 100/72 = 1.39 px there, so 0.3 pt is 0.42 px of line before alpha);
# secondary highways left exactly at today's draw_basemap() values, keeping
# them as faint background texture rather than a second competing network.
ROADS = {
    "secondary": {"color": "0.4", "lw": 0.3, "alpha": 0.35, "zorder": 5},
    "major": {"color": "0.4", "lw": 0.575, "alpha": 1.0, "zorder": 6},
}


def _fill(ax, lons, lats, values):
    return ax.contourf(
        lons, lats, values, levels=LEVELS, cmap=CMAP,
        norm=mcolors.Normalize(vmin=LEVELS[0], vmax=LEVELS[-1]),
        extend="both", zorder=FIELD_Z,
    )


def _edges(ax, lons, lats, values, color="0.25", lw=0.25):
    """Thin outline on every band boundary. On a banded fill this is what
    makes the isotherm positions legible instead of merely inferrable from a
    colour step, and it costs almost nothing at dpi=100."""
    ax.contour(lons, lats, values, levels=LEVELS, colors=[color], linewidths=lw,
               zorder=FIELD_Z + 0.1)


def _emphasis_isolines(ax, lons, lats, values, color="black", lw=0.9):
    """On a 2 C banded fill every boundary looks alike, so there is no anchor
    for reading a value off the map without counting bands from the
    colourbar. Emphasising 20/30/40 gives the eye three fixed reference
    contours to count from - the usual met-chart convention."""
    ax.contour(lons, lats, values, levels=EMPHASIS_ISOTHERMS, colors=[color],
               linewidths=lw, zorder=FIELD_Z + 0.15)


def _road_tier(ax, bbox, tier: str, spec: dict) -> None:
    gdf = _clip(_load_roads(tier), bbox)
    if gdf.empty:
        return
    gdf.plot(ax=ax, color=spec["color"], linewidth=spec["lw"],
             alpha=spec["alpha"], zorder=spec["zorder"])


def draw_overlays(ax, bbox) -> None:
    """draw_basemap()'s coastline unchanged (black, 0.5 pt) + the F11 roads +
    the C2 totality overlay (plain black, NO halo).

    src/viz/basemap.py is shared with the production cloud renderer and is
    deliberately not touched, so only the drawing half of draw_basemap() is
    re-implemented here - its loaders/clipper (_load_land/_load_roads/_clip)
    are reused so geometry and the Natural Earth cache are identical to
    production. A production version of this belongs as a STYLE PARAMETER on
    draw_basemap(), not as a changed default."""
    _clip(_load_land(), bbox).boundary.plot(ax=ax, color="black", linewidth=0.5, zorder=5)
    _road_tier(ax, bbox, "Secondary Highway", ROADS["secondary"])
    _road_tier(ax, bbox, "Major Highway", ROADS["major"])
    ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "-", color="k",
            linewidth=0.8, alpha=0.85, zorder=7)
    ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "--", color="k",
            linewidth=1.1, alpha=0.95, zorder=7)


def draw_colorbar(ax, mappable) -> None:
    """Compact horizontal key over the bottom-left corner (Atlantic/Alboran).
    A temperature map without a key is unreadable in a way a 0-100% cloud map
    is not - the viewer has no prior for which colour is 30 C."""
    ax.add_patch(
        Rectangle(
            (0.022, 0.022), 0.50, 0.115, transform=ax.transAxes,
            facecolor="white", alpha=0.78, edgecolor="0.5", linewidth=0.4, zorder=8,
        )
    )
    cax = ax.inset_axes([0.045, 0.052, 0.45, 0.026], zorder=9)
    cbar = ax.figure.colorbar(mappable, cax=cax, orientation="horizontal", ticks=TICKS)
    cbar.ax.tick_params(labelsize=4.4, length=1.4, width=0.35, pad=1.0, colors="#222")
    cbar.outline.set_linewidth(0.3)
    ax.text(0.045, 0.098, "2 m temperature (C)", transform=ax.transAxes, fontsize=4.8,
            ha="left", va="bottom", color="#222", zorder=9)


def render_panel(model_id: str, title: str, lats, lons, values, bbox: dict,
                 dpi: int = 100) -> str:
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    cs = _fill(ax, lons, lats, values)
    _edges(ax, lons, lats, values)
    _emphasis_isolines(ax, lons, lats, values)
    draw_colorbar(ax, cs)
    draw_overlays(ax, bbox)

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, fontsize=6.5)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{model_id}_{RUN_INIT:%Y%m%d%H}_f{STEP:03d}_t2m.png"
    fig.savefig(PANEL_DIR / name, dpi=dpi)
    plt.close(fig)
    return f"{PANEL_DIR.name}/{name}"


# ---------------------------------------------------------------------------
# Model selection - from config, never a hardcoded list
# ---------------------------------------------------------------------------


def two_metre_models() -> list[dict]:
    """Every models.yaml entry whose surface_temp is a genuine 2 m field, in
    file order. `height` is the discriminator: arome_france/arpege_europe
    carry `height: skin` (T45) and are excluded by it automatically, which is
    exactly why that key exists."""
    models = load_models()["models"]
    out = []
    for model_id, config in models.items():
        surface_temp = config.get("surface_temp") or {}
        if surface_temp.get("height") != "2m":
            continue
        grid = config.get("grid") or {}
        if "deg" in grid:
            resolution = f"{grid['deg']}&deg;"
        elif "km" in grid:
            resolution = f"~{grid['km']} km"
        else:
            resolution = ""
        out.append({
            "id": model_id,
            "provider": config.get("provider", ""),
            "kind": config.get("kind", ""),
            "resolution": resolution,
            "param": surface_temp.get("param", ""),
            "renderable": model_id in READERS,
        })
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    CACHE.mkdir(parents=True, exist_ok=True)
    bbox = eclipse_config()["bbox"]

    panels = []
    for entry in two_metre_models():
        model_id = entry["id"]
        if not entry["renderable"]:
            panels.append({
                **entry, "png": None, "members": None,
                "error": "no gridded reader - Open-Meteo point model, out of scope for a map",
            })
            log.info("%-14s SKIPPED (no gridded reader)", model_id)
            continue
        try:
            log.info("%-14s fetching ...", model_id)
            result = READERS[model_id](bbox)
            members = result[3] if len(result) > 3 else 1
            lats, lons, values = result[0], result[1], result[2]
            title = (f"{model_id} - {_fmt_dm_z(RUN_INIT)} -> {_fmt_dm_z(VALID)} "
                     f"(+{STEP}h) - 2 m temperature")
            png = render_panel(model_id, title, lats, lons, values, bbox)
            finite = values[np.isfinite(values)]
            # gefs_extended's models.yaml kind IS "ensemble" (31 members
            # upstream), but this project's fetcher only ever pulls the c00
            # control member - so this panel is a single member, not a mean,
            # and must not be labelled as one.
            member_kind = (
                f"ensemble mean of {members} members" if members > 1
                else "control member only (this project fetches c00)"
                if str(entry["kind"]).startswith("ensemble")
                else "deterministic"
            )
            panels.append({
                **entry,
                "png": png,
                "members": int(members),
                "ensemble_mean": bool(members > 1),
                "member_kind": member_kind,
                "shape": [int(values.shape[0]), int(values.shape[1])],
                "min_c": round(float(finite.min()), 1),
                "max_c": round(float(finite.max()), 1),
                "mean_c": round(float(finite.mean()), 1),
                "error": None,
            })
            log.info("%-14s ok  %s  %.1f .. %.1f C  (%d member(s))",
                     model_id, list(values.shape), finite.min(), finite.max(), members)
        except Exception as exc:  # noqa: BLE001 - one dead model must not kill the page
            panels.append({**entry, "png": None, "members": None,
                           "error": f"{type(exc).__name__}: {exc}"})
            log.warning("%-14s FAILED: %s: %s", model_id, type(exc).__name__, exc)

    grid = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "run_init": RUN_INIT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_init_short": _fmt_dm_z(RUN_INIT),
        "valid": VALID.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_short": _fmt_dm_z(VALID),
        "step": STEP,
        "scale": {"cmap": CMAP, "vmin": float(LEVELS[0]), "vmax": float(LEVELS[-1]),
                  "band_c": 2.0, "emphasis": EMPHASIS_ISOTHERMS, "dpi": 100},
        "panels": panels,
    }
    GRID_JSON.parent.mkdir(parents=True, exist_ok=True)
    GRID_JSON.write_text(json.dumps(grid, indent=2), encoding="utf-8")
    ok = sum(1 for p in panels if p["png"])
    log.info("wrote %s (%d/%d panels rendered)", GRID_JSON, ok, len(panels))


if __name__ == "__main__":
    main()
