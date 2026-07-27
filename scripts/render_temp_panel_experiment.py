"""Design experiment (NOT production): how should a SURFACE-TEMPERATURE panel
look?

Temperature is a STANDALONE panel here, not an overlay - unlike
scripts/render_rain_overlay_experiment.py, which had to survive on top of two
existing cloud backgrounds. Nothing else is drawn underneath, so the full
colour channel is available and `contourf` owns the whole map.

Two rendering modes are explored, because they answer different questions and
almost certainly want different colour schemes:

  (a) ABSOLUTE 2 m temperature. "How hot is it where I'm standing." Needs a
      FIXED scale - per-frame autoscaling makes models incomparable and makes
      the run-evolution slider flicker (candidate A12 demonstrates exactly
      that failure with real data).
  (b) ANOMALY / difference on a symmetric diverging scale about zero. This is
      the scientifically important one: only ECMWF HRES/ENS simulate the
      eclipse's solar obscuration (IFS Cycle 50r1), with a documented local
      2 m cooling of up to 7 C. An absolute map cannot show that - see the
      real inter-model-difference column, which measures the spread this
      experiment claims would swamp it.

Real data, four fetches, all at the same valid time so the columns are
directly comparable:

  * GFS 0.25 deg      - TMP at "2 m above ground", herbie idx byte-range.
  * ICON-EU 0.0625 deg- T_2M, one small .grib2.bz2 per step from opendata.dwd.de
                        (~1 MB), the same URL convention dwd_bz2_fetcher.py uses.
  * ECMWF HRES 0.25   - param "2t" via ecmwf-opendata. NOTE the naming
                        correction found 2026-07-27: models.yaml says `t2m`,
                        but the ECMWF family's FETCH/filter-side name is `2t`;
                        cfgrib then names the resulting variable `t2m`. Both
                        names are needed, for different layers.
  * GFS +27h (03Z)    - the cold end of the diurnal cycle, purely to stress
                        the fixed absolute scale at the opposite extreme.

Deliberately NOT used: arome_france/arpege_europe's SP2 `t`, verified
2026-07-27 to be SKIN temperature, not 2 m - roughly double the diurnal
amplitude, so it would misrepresent any shared scale.

THE ANOMALY FIELD IS PART REAL, PART SYNTHETIC - stated plainly on the review
page too:
  * "diurnal curvature" column: 100% REAL. T(18Z) - (T(15Z)+T(21Z))/2 from one
    GFS run, i.e. exactly the T(18Z) - baseline(15Z,21Z) the project's 15/18/21
    archive hours were chosen to support. It contains NO eclipse - GFS is
    eclipse-blind - so it measures the NON-eclipse background this anomaly
    definition carries.
  * "with eclipse" column: that real field PLUS A SYNTHETIC umbral cooling.
    No real eclipse forecast exists yet (2026-08-12 is 16 days out, beyond
    HRES's 10-day range), so the -7 C dip is fabricated for design purposes
    only: a Gaussian in distance from the real totality centreline, deepened
    toward the west (the umbra passes there earliest, so surface cooling has
    had longest to develop) and masked to land by the model's own diurnal
    amplitude. It is a plausible SHAPE at a documented MAGNITUDE, nothing more.
  * "model spread" column: 100% REAL. GFS(18Z) - ICON-EU(18Z) at the same
    valid hour, bilinearly regridded - the actual inter-model disagreement the
    eclipse signal would have to compete with on an absolute map.

frame_renderer.py is deliberately NOT modified, and none of its geometry is
re-derived: _figure_layout(), the 1.3 aspect, dpi, the totality path arrays and
_fmt_dm_z are all imported from it so every panel here is pixel-comparable to a
production frame.

Usage (inside the Docker container, GRIB deps required):
    docker cp scripts/render_temp_panel_experiment.py eclipse-scheduler:/app/scripts/
    docker exec eclipse-scheduler /app/.venv/bin/python \
        -m scripts.render_temp_panel_experiment          # whole page
    docker exec eclipse-scheduler /app/.venv/bin/python \
        -m scripts.render_temp_panel_experiment roads    # section (f) only, spliced in
"""

from __future__ import annotations

import bz2
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import cfgrib
import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from herbie import Herbie
from matplotlib.patches import Rectangle

from src.config import DATA_ROOT, eclipse_config
from src.viz.basemap import _clip, _load_land, _load_roads, draw_basemap
from src.viz.cloud_field_comparison import _crop
from src.viz.frame_renderer import (
    OUTPUT_DIR,
    _CLOUD_GAMMA,
    _TOTALITY_BAND_LAT,
    _TOTALITY_BAND_LON,
    _TOTALITY_CENTER_LAT,
    _TOTALITY_CENTER_LON,
    _figure_layout,
    _fmt_dm_z,
)

log = logging.getLogger(__name__)

EXPERIMENT_DIR = OUTPUT_DIR / "temp_panel_experiment"
GRID_JSON = OUTPUT_DIR / "temp_panel_grid.json"
CACHE = DATA_ROOT / "cache" / "temp_experiment"

RUN_INIT = datetime(2026, 7, 27, 0)
STEPS = (15, 18, 21)
# The SAME three hours one day later, from the same run. Their diurnal
# curvature is a second eclipse-free sample of the same quantity, which is what
# makes the "double difference" baseline in the (b0) section measurable.
STEPS_NEXT_DAY = (39, 42, 45)
NIGHT_STEP = 27  # 03Z next morning - the cold end of the same run

_DWD_URL = (
    "https://opendata.dwd.de/weather/nwp/icon-eu/grib/{hh}/t_2m/"
    "icon-eu_europe_regular-lat-lon_single-level_{yyyymmddhh}_{fff}_T_2M.grib2.bz2"
)
_UA = "eclipse-weather-archiver/0.1 (contact: lauri@farsight.space)"

KELVIN = 273.15


# ---------------------------------------------------------------------------
# Data access - every reader returns (lats, lons, degrees Celsius)
# ---------------------------------------------------------------------------


def _to_iberia(ds: xr.Dataset, var: str, bbox: dict) -> tuple:
    """Crop to the Iberia bbox, converting a 0-360 grid to -180..180 first.
    ICON-EU and HRES are already -180..180 (checked live); GFS is not."""
    lons = ds.longitude.values.copy()
    if lons.max() > 180:
        lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    return _crop(ds.latitude.values, lons[order], ds[var].values[:, order], bbox)


def read_gfs(step: int, bbox: dict) -> tuple:
    h = Herbie(
        RUN_INIT, model="gfs", product="pgrb2.0p25", fxx=step,
        save_dir=str(CACHE), verbose=False,
    )
    # One clean instantaneous message per step - unlike cloud/rain, TMP at 2 m
    # has no windowed-average twin to exclude (models.yaml T37 note).
    path = h.download(search=r":TMP:2 m above ground:\d+ hour fcst:", verbose=False)
    ds = cfgrib.open_datasets(str(path))[0]
    lats, lons, values = _to_iberia(ds, "t2m", bbox)
    return lats, lons, values - KELVIN


def read_icon_eu(step: int, bbox: dict) -> tuple:
    dst = CACHE / f"icon_eu_{RUN_INIT:%Y%m%d%H}_{step:03d}_T_2M.grib2"
    if not dst.exists():
        url = _DWD_URL.format(
            hh=f"{RUN_INIT:%H}", yyyymmddhh=f"{RUN_INIT:%Y%m%d%H}", fff=f"{step:03d}"
        )
        resp = httpx.get(url, timeout=120.0, follow_redirects=True, headers={"User-Agent": _UA})
        resp.raise_for_status()
        dst.write_bytes(bz2.decompress(resp.content))
    ds = xr.open_dataset(str(dst), engine="cfgrib")
    lats, lons, values = _to_iberia(ds, "t2m", bbox)
    return lats, lons, values - KELVIN


def read_hres(step: int, bbox: dict) -> tuple:
    from ecmwf.opendata import Client

    dst = CACHE / f"hres_{RUN_INIT:%Y%m%d%H}_2t_f{step:03d}.grib2"
    if not dst.exists():
        # "2t" on the request side; cfgrib names the variable "t2m". models.yaml
        # currently records only the latter - see the module docstring.
        Client().retrieve(
            request={
                "stream": "oper", "type": "fc", "date": RUN_INIT.date(),
                "time": RUN_INIT.hour, "step": step, "param": "2t",
            },
            target=str(dst),
        )
    ds = xr.open_dataset(str(dst), engine="cfgrib")
    lats, lons, values = _to_iberia(ds, "t2m", bbox)
    return lats, lons, values - KELVIN


def regrid(src: tuple, dst_lats: np.ndarray, dst_lons: np.ndarray) -> np.ndarray:
    """Bilinear resample of (lats, lons, values) onto another regular lat/lon
    grid, so two models at different resolutions can be differenced pixelwise.
    Hand-rolled rather than scipy.interpolate - the container image has no
    scipy, and adding one to the archiver's dependency set for a design
    experiment would be the wrong trade."""
    lats, lons, values = src
    if lats[0] > lats[-1]:
        lats, values = lats[::-1], values[::-1, :]
    if lons[0] > lons[-1]:
        lons, values = lons[::-1], values[:, ::-1]

    yi = np.clip(np.searchsorted(lats, dst_lats) - 1, 0, len(lats) - 2)
    xi = np.clip(np.searchsorted(lons, dst_lons) - 1, 0, len(lons) - 2)
    ty = np.clip((dst_lats - lats[yi]) / (lats[yi + 1] - lats[yi]), 0, 1)[:, None]
    tx = np.clip((dst_lons - lons[xi]) / (lons[xi + 1] - lons[xi]), 0, 1)[None, :]

    v00 = values[np.ix_(yi, xi)]
    v01 = values[np.ix_(yi, xi + 1)]
    v10 = values[np.ix_(yi + 1, xi)]
    v11 = values[np.ix_(yi + 1, xi + 1)]
    return v00 * (1 - ty) * (1 - tx) + v01 * (1 - ty) * tx + v10 * ty * (1 - tx) + v11 * ty * tx


# ---------------------------------------------------------------------------
# The synthetic eclipse cooling - SYNTHETIC, see module docstring
# ---------------------------------------------------------------------------

_ECLIPSE_PEAK_C = -7.0     # documented IFS Cy50r1 local 2 m cooling
_UMBRA_SIGMA_KM = 110.0    # ~half the totality band half-width
_PENUMBRA_PEAK_C = -2.2    # broad partial-obscuration cooling
_PENUMBRA_SIGMA_KM = 420.0


def _km_to_centerline(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Great-circle-ish distance (km) from every grid point to the nearest
    vertex of the REAL 2026-08-12 totality centreline (config/totality_path.json,
    via frame_renderer's own arrays). Vertex distance, not segment distance -
    the path is sampled finely enough that the difference is far below the
    110 km Gaussian width used above."""
    clat = np.asarray(_TOTALITY_CENTER_LAT)
    clon = np.asarray(_TOTALITY_CENTER_LON)
    glat, glon = np.meshgrid(lats, lons, indexing="ij")
    dlat = (glat[..., None] - clat) * 111.32
    dlon = (glon[..., None] - clon) * 111.32 * np.cos(np.radians(glat[..., None]))
    return np.sqrt(dlat**2 + dlon**2).min(axis=-1)


def synth_eclipse_cooling(lats: np.ndarray, lons: np.ndarray, land: np.ndarray) -> np.ndarray:
    """SYNTHETIC. Umbral + penumbral Gaussian in distance from the real
    centreline, deepened toward the west (the umbra reaches Galicia first, so
    by a fixed snapshot the surface there has been cooling longest), and scaled
    by a land mask - sea-surface temperature has far too much thermal inertia
    to respond in the ~10 min the shadow takes to pass."""
    dist = _km_to_centerline(lats, lons)
    _, glon = np.meshgrid(lats, lons, indexing="ij")
    # 1.0 at the western edge of the bbox -> 0.6 at the eastern edge.
    west_bias = 1.0 - 0.4 * (glon - lons.min()) / (lons.max() - lons.min())
    umbra = _ECLIPSE_PEAK_C * np.exp(-((dist / _UMBRA_SIGMA_KM) ** 2))
    penumbra = _PENUMBRA_PEAK_C * np.exp(-((dist / _PENUMBRA_SIGMA_KM) ** 2))
    return (umbra + penumbra) * west_bias * land


def land_mask_from_diurnal(t15: np.ndarray, t21: np.ndarray) -> np.ndarray:
    """0..1 "landiness", derived from the model's own 15Z->21Z cooling instead
    of a shapefile: Iberian land drops 5-12 C over those six hours, the Atlantic
    and Mediterranean barely half a degree. Keeps the synthetic field consistent
    with the very grid it is being added to, at no extra dependency."""
    return np.clip((np.abs(t15 - t21) - 1.0) / 4.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Norms
# ---------------------------------------------------------------------------


class SymPowerNorm(mcolors.Normalize):
    """A symmetric power stretch about zero - what you get if the production
    _gamma_for_field() habit of PowerNorm(gamma=0.40) is carried over to a
    diverging anomaly. Included only to be rejected: it expands the |dT| < 1 C
    region, which on an anomaly field is almost entirely numerical/interpolation
    noise, into most of the colour range."""

    def __init__(self, gamma: float, halfrange: float):
        super().__init__(vmin=-halfrange, vmax=halfrange, clip=False)
        self.gamma = gamma
        self.halfrange = halfrange

    def __call__(self, value, clip=None):
        x = np.clip(np.asarray(value, dtype=float) / self.halfrange, -1, 1)
        return np.ma.masked_invalid(0.5 + 0.5 * np.sign(x) * np.abs(x) ** self.gamma)

    def inverse(self, value):
        y = 2.0 * (np.asarray(value, dtype=float) - 0.5)
        return np.sign(y) * np.abs(y) ** (1.0 / self.gamma) * self.halfrange


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

FIELD_Z = 1


def _fill(ax, lons, lats, values, *, levels, cmap, norm=None, extend="both"):
    norm = norm or mcolors.Normalize(vmin=levels[0], vmax=levels[-1])
    return ax.contourf(
        lons, lats, values, levels=levels, cmap=cmap, norm=norm,
        extend=extend, zorder=FIELD_Z,
    )


def _edges(ax, lons, lats, values, levels, color="0.25", lw=0.25):
    """Thin outline on every band boundary. On a banded fill this is what makes
    the isotherm positions legible instead of merely inferrable from a colour
    step, and it costs almost nothing at dpi=100."""
    ax.contour(lons, lats, values, levels=levels, colors=[color], linewidths=lw,
               zorder=FIELD_Z + 0.1)


def _labelled_isolines(ax, lons, lats, values, levels, color="0.15", fmt="%g", fontsize=4.4):
    cs = ax.contour(lons, lats, values, levels=levels, colors=[color],
                    linewidths=0.55, zorder=FIELD_Z + 0.2)
    labels = ax.clabel(cs, inline=True, fmt=fmt, fontsize=fontsize)
    for text in labels:
        text.set_bbox(dict(facecolor="white", alpha=0.55, edgecolor="none", pad=0.4))
    return cs


# --- road styling (section (f)) -------------------------------------------
# src/viz/basemap.py is SHARED WITH THE PRODUCTION CLOUD RENDERER and is
# deliberately not touched here, so this experiment re-implements only the
# drawing half of draw_basemap() locally, reusing its loaders/clipper
# (_load_land/_load_roads/_clip) so the geometry and the Natural Earth cache
# are byte-identical to production. A production version of whatever wins
# should be a STYLE PARAMETER on draw_basemap(), not a changed default -
# see the write-up.

# Today's hardcoded draw_basemap() values, kept here as the section's control.
BASEMAP_DEFAULT_ROADS = {
    "secondary": {"color": "0.4", "lw": 0.3, "alpha": 0.35, "zorder": 5},
    "major": {"color": "0.4", "lw": 0.3, "alpha": 0.8, "zorder": 6},
}


def _road_tier(ax, bbox, tier: str, spec: dict | None) -> None:
    """One Natural Earth road tier with an optional casing/halo stroke.

    `casing` is the OUTLINE colour drawn under the stroke (matplotlib's
    patheffects.withStroke - the same mechanism the accepted C3 totality
    overlay uses, run in the opposite direction: a light core inside a dark
    casing rather than a dark core inside a light halo)."""
    if spec is None:
        return
    gdf = _clip(_load_roads(tier), bbox)
    if gdf.empty:
        return
    gdf.plot(ax=ax, color=spec["color"], linewidth=spec["lw"],
             alpha=spec.get("alpha", 1.0), zorder=spec["zorder"])
    if spec.get("casing"):
        ax.collections[-1].set_path_effects([
            pe.withStroke(linewidth=spec["casing_lw"], foreground=spec["casing"],
                          alpha=spec.get("casing_alpha", 1.0))
        ])


def draw_basemap_styled(ax, bbox, roads: dict) -> None:
    """draw_basemap()'s coastline, unchanged, plus roads drawn to `roads`.

    The coastline is held at production's black / 0.5 pt / zorder 5 in EVERY
    row of section (f) so the only variable in the comparison is the roads."""
    _clip(_load_land(), bbox).boundary.plot(ax=ax, color="black", linewidth=0.5, zorder=5)
    _road_tier(ax, bbox, "Secondary Highway", roads.get("secondary"))
    _road_tier(ax, bbox, "Major Highway", roads.get("major"))


def draw_overlays(ax, bbox, *, color="k", halo=False, roads=None):
    if roads is None:
        draw_basemap(ax, bbox)
    else:
        draw_basemap_styled(ax, bbox, roads)
    band = ax.plot(_TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, "-", color=color,
                   linewidth=1.0 if halo else 0.8, alpha=0.85, zorder=7)
    center = ax.plot(_TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, "--", color=color,
                     linewidth=1.3 if halo else 1.1, alpha=0.95, zorder=7)
    if halo:
        for artist in band + center:
            artist.set_path_effects(
                [pe.withStroke(linewidth=2.4, foreground="white", alpha=0.8)]
            )


def draw_colorbar(ax, mappable, ticks, label):
    """Compact horizontal key over the bottom-left corner (Atlantic/Alboran),
    same corner the rain experiment put its banded key in. A temperature map
    without a key is unreadable in a way a 0-100% cloud map is not - the viewer
    has no prior for which colour is 30 C."""
    ax.add_patch(
        Rectangle(
            (0.022, 0.022), 0.50, 0.115, transform=ax.transAxes,
            facecolor="white", alpha=0.78, edgecolor="0.5", linewidth=0.4, zorder=8,
        )
    )
    cax = ax.inset_axes([0.045, 0.052, 0.45, 0.026], zorder=9)
    cbar = ax.figure.colorbar(mappable, cax=cax, orientation="horizontal", ticks=ticks)
    cbar.ax.tick_params(labelsize=4.4, length=1.4, width=0.35, pad=1.0, colors="#222")
    cbar.outline.set_linewidth(0.3)
    ax.text(0.045, 0.098, label, transform=ax.transAxes, fontsize=4.8,
            ha="left", va="bottom", color="#222", zorder=9)


# ---------------------------------------------------------------------------
# (a) ABSOLUTE candidates
# ---------------------------------------------------------------------------

# The production-scale proposal: 0-44 C in 2 C bands. Wide enough to survive
# every step of a summer run without clipping (real GFS/ICON/HRES numbers this
# session: 18Z max 39.6 C, 03Z min 8.6 C) and every model on the same ramp.
ABS_LEVELS_2 = np.arange(0, 45, 2.0)
ABS_LEVELS_1 = np.arange(0, 44.5, 1.0)
ABS_LEVELS_5 = np.arange(0, 50, 5.0)
ABS_LEVELS_NARROW = np.arange(14, 41, 2.0)  # the "just the eclipse hour" scale
ABS_CONT = np.linspace(0, 44, 160)
ABS_TICKS = np.arange(0, 45, 5.0)


def _abs_cell(ax, lons, lats, v, *, levels, cmap, norm=None, edges=False,
              iso=None, ticks=ABS_TICKS, label="2 m temperature (C)"):
    cs = _fill(ax, lons, lats, v, levels=levels, cmap=cmap, norm=norm)
    if edges:
        _edges(ax, lons, lats, v, levels)
    if iso is not None:
        _labelled_isolines(ax, lons, lats, v, iso, fmt="%g")
    draw_colorbar(ax, cs, ticks, label)


ABS_CANDIDATES = [
    {
        "id": "a01_turbo",
        "label": "A1. turbo, continuous, 0-44 C",
        "note": "The reflex rainbow. Maximum apparent detail, but the hue order is not a "
                "perceptual order - it invents banding where the field is smooth and hides "
                "real gradients in the green plateau.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_CONT, cmap="turbo"),
    },
    {
        "id": "a02_inferno",
        "label": "A2. inferno, continuous, 0-44 C",
        "note": "Perceptually uniform sequential. Monotone in lightness, so it survives "
                "greyscale and every CVD type, but 'cold = black' reads as missing data.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_CONT, cmap="inferno"),
    },
    {
        "id": "a03_viridis",
        "label": "A3. viridis, continuous, 0-44 C",
        "note": "The other perceptual default. Same lightness virtue; 'hot = yellow, cold = "
                "purple' has no temperature convention behind it at all.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_CONT, cmap="viridis"),
    },
    {
        "id": "a04_ylorrd",
        "label": "A4. YlOrRd, 2 C bands, 0-44 C",
        "note": "Sequential warm ramp. Conventionally 'hot', monotone in lightness - but the "
                "whole cool half of Iberia collapses into near-white.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2, cmap="YlOrRd"),
    },
    {
        "id": "a05_rdylbu",
        "label": "A5. RdYlBu_r, 2 C bands, 0-44 C",
        "note": "The classic meteorological temperature ramp: blue-cold / yellow-mild / "
                "red-hot. Diverging in hue about the mid-scale but sequential in meaning.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2, cmap="RdYlBu_r"),
    },
    {
        "id": "a06_rdylbu_edges",
        "label": "A6. RdYlBu_r, 2 C bands + band edges",
        "note": "A5 plus a 0.25 pt outline on every isotherm - the band boundary becomes a "
                "readable line instead of only a colour step.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2,
                                              cmap="RdYlBu_r", edges=True),
    },
    {
        "id": "a07_rdylbu_1c",
        "label": "A7. RdYlBu_r, 1 C bands",
        "note": "Spacing test, finer. 44 bands: adjacent colours become indistinguishable, "
                "so it is a continuous ramp in disguise with none of its smoothness.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_1, cmap="RdYlBu_r"),
    },
    {
        "id": "a08_rdylbu_5c",
        "label": "A8. RdYlBu_r, 5 C bands",
        "note": "Spacing test, coarser. Very legible, very quantised - a 4 C difference "
                "between two models can vanish entirely into one band.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_5,
                                              cmap="RdYlBu_r", edges=True),
    },
    {
        "id": "a09_rdylbu_cont",
        "label": "A9. RdYlBu_r, continuous",
        "note": "Same hues, 160 levels. Prettier and truer to a smooth field, but you cannot "
                "read a value off it without the key.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_CONT, cmap="RdYlBu_r"),
    },
    {
        "id": "a10_rdylbu_clabel",
        "label": "A10. RdYlBu_r 2 C bands + labelled 5 C isotherms",
        "note": "A6 with inline clabel isotherms every 5 C. The line channel is free on a "
                "standalone panel - the question is whether it earns its clutter at dpi=100.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2,
                                              cmap="RdYlBu_r", edges=True,
                                              iso=np.arange(5, 45, 5.0)),
    },
    {
        "id": "a11_narrow",
        "label": "A11. RdYlBu_r 2 C bands, NARROW 14-40 C scale",
        "note": "Scale-limit test: tuned to the eclipse hour only. More contrast at 18Z - and "
                "the 03Z column shows what it does to every other step of the same run.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_NARROW,
                                              cmap="RdYlBu_r", edges=True,
                                              ticks=np.arange(14, 41, 4.0)),
    },
    {
        "id": "a12_autoscale",
        "label": "A12. RdYlBu_r, PER-FRAME AUTOSCALE (anti-pattern)",
        "note": "vmin/vmax from each frame's own data. Every panel looks identical and means "
                "something different - this is the flicker the run-evolution slider must not have.",
        "draw": None,  # needs the data to build its own levels - special-cased below
    },
    {
        "id": "a14_rdbu",
        "label": "A14. RdBu_r, 2 C bands, 0-44 C",
        "note": "The anomaly panel's own ramp, re-used for absolute with the scale midpoint "
                "(22 C) at its white centre. Keeps blue=cold/red=hot and, crucially, contains "
                "no yellow - compare its deuteranopia column against A6's.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2,
                                              cmap="RdBu_r", edges=True),
    },
    {
        "id": "a15_coolwarm",
        "label": "A15. coolwarm, 2 C bands, 0-44 C",
        "note": "Same idea with Moreland's ramp: light grey rather than white at 22 C, lower "
                "chroma at the ends. Less shouty than A14, less separable at the extremes.",
        "draw": lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2,
                                              cmap="coolwarm", edges=True),
    },
    {
        "id": "a13_gamma",
        "label": "A13. YlOrRd with production PowerNorm(gamma=0.40) (anti-pattern)",
        "note": "What _gamma_for_field() returns today for any non-prob_ field. A power stretch "
                "encodes 'ratio to vmax', which is meaningless for a Celsius scale whose zero "
                "is arbitrary: 0-15 C gets over half the colour range.",
        "draw": lambda ax, x, y, v: _abs_cell(
            ax, x, y, v, levels=ABS_CONT, cmap="YlOrRd",
            norm=mcolors.PowerNorm(gamma=_CLOUD_GAMMA, vmin=0, vmax=44),
        ),
    },
]


def _draw_autoscale(ax, lons, lats, v):
    lo = float(np.floor(np.nanmin(v)))
    hi = float(np.ceil(np.nanmax(v)))
    levels = np.linspace(lo, hi, 23)
    cs = _fill(ax, lons, lats, v, levels=levels, cmap="RdYlBu_r")
    _edges(ax, lons, lats, v, levels)
    draw_colorbar(ax, cs, np.linspace(lo, hi, 5), f"2 m temperature (C) - AUTOSCALED {lo:g}..{hi:g}")


# ---------------------------------------------------------------------------
# (b) ANOMALY candidates
# ---------------------------------------------------------------------------

ANOM_HALF = 8.0
ANOM_LEVELS_1 = np.arange(-8, 8.5, 1.0)
ANOM_LEVELS_05 = np.arange(-8, 8.25, 0.5)
ANOM_LEVELS_2 = np.arange(-8, 8.5, 2.0)
ANOM_LEVELS_4 = np.arange(-4, 4.5, 0.5)
ANOM_CONT = np.linspace(-8, 8, 161)
ANOM_TICKS = np.arange(-8, 8.5, 2.0)
# Deadband: no fill inside +-0.5 C. contourf draws nothing below its lowest
# level and nothing between two level lists, so splitting the level array in
# two leaves the near-zero region uncoloured.
ANOM_DEAD_NEG = np.concatenate([np.arange(-8, 0, 1.0), [-0.5]])
ANOM_DEAD_POS = np.concatenate([[0.5], np.arange(1, 9, 1.0)])


def _anom_cell(ax, lons, lats, v, *, levels, cmap, norm=None, edges=False, iso=None,
               ticks=ANOM_TICKS, label="2 m temperature anomaly (C)"):
    cs = _fill(ax, lons, lats, v, levels=levels, cmap=cmap, norm=norm)
    if edges:
        _edges(ax, lons, lats, v, levels, color="0.30", lw=0.22)
    if iso is not None:
        _labelled_isolines(ax, lons, lats, v, iso, fmt="%+g")
    draw_colorbar(ax, cs, ticks, label)


def _anom_deadband(ax, lons, lats, v):
    norm = mcolors.Normalize(vmin=-ANOM_HALF, vmax=ANOM_HALF)
    ax.contourf(lons, lats, v, levels=ANOM_DEAD_NEG, cmap="RdBu_r", norm=norm,
                extend="min", zorder=FIELD_Z)
    ax.contourf(lons, lats, v, levels=ANOM_DEAD_POS, cmap="RdBu_r", norm=norm,
                extend="max", zorder=FIELD_Z)
    ax.contour(lons, lats, v, levels=[-0.5, 0.5], colors=["0.45"], linewidths=0.3,
               zorder=FIELD_Z + 0.1)
    # Neither half's own ContourSet spans the full scale, so the key is built
    # from a standalone mappable over the whole +-8 C range instead.
    mappable = plt.cm.ScalarMappable(
        norm=mcolors.BoundaryNorm(ANOM_LEVELS_1, 256), cmap="RdBu_r"
    )
    mappable.set_array([])
    draw_colorbar(ax, mappable, ANOM_TICKS, "anomaly (C), |dT| < 0.5 left blank")


ANOM_CANDIDATES = [
    {
        "id": "b01_bwr",
        "label": "B1. bwr, +-8 C, 1 C bands",
        "note": "Pure blue-white-red. Maximum saturation at both ends; the white centre is "
                "the same white as the page, so 'no anomaly' and 'no data' look alike.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1, cmap="bwr"),
    },
    {
        "id": "b02_rdbu",
        "label": "B2. RdBu_r, +-8 C, 1 C bands",
        "note": "ColorBrewer diverging red-blue: the reference symmetric scale, and one of "
                "the safest choices for deuteranopia (both poles differ in blue-yellow).",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1, cmap="RdBu_r"),
    },
    {
        "id": "b03_rdbu_edges",
        "label": "B3. RdBu_r, +-8 C, 1 C bands + band edges",
        "note": "B2 plus 0.22 pt band outlines. On a smooth anomaly field the outlines are "
                "what let you count degrees down the gradient.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1,
                                               cmap="RdBu_r", edges=True),
    },
    {
        "id": "b04_rdbu_05",
        "label": "B4. RdBu_r, +-8 C, 0.5 C bands",
        "note": "Spacing test, finer. 32 bands over the range; the real (non-eclipse) "
                "curvature column shows how much of that is structure you do not want lit up.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_05, cmap="RdBu_r"),
    },
    {
        "id": "b05_rdbu_2",
        "label": "B5. RdBu_r, +-8 C, 2 C bands",
        "note": "Spacing test, coarser. Four bands per side - clean, but a 7 C dip and a 5 C "
                "dip land two bands apart, which is all the resolution you get.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_2,
                                               cmap="RdBu_r", edges=True),
    },
    {
        "id": "b06_rdbu_cont",
        "label": "B6. RdBu_r, +-8 C, continuous",
        "note": "161 levels. Smooth, but the eclipse question is quantitative ('is it 7 C or "
                "2 C') and a continuous ramp answers it worst.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_CONT, cmap="RdBu_r"),
    },
    {
        "id": "b07_coolwarm",
        "label": "B7. coolwarm, +-8 C, 1 C bands",
        "note": "Moreland's diverging map: no white centre (light grey), so zero never reads "
                "as blank. Lower chroma at the poles than RdBu_r, so extremes shout less.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1,
                                               cmap="coolwarm", edges=True),
    },
    {
        "id": "b08_puor",
        "label": "B8. PuOr, +-8 C, 1 C bands",
        "note": "Purple/orange - the CVD-safest diverging pair of all, but it discards the "
                "blue=cold / red=warm convention the absolute panel just taught the viewer.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1,
                                               cmap="PuOr", edges=True),
    },
    {
        "id": "b09_deadband",
        "label": "B9. RdBu_r +-8 C with a +-0.5 C deadband",
        "note": "Nothing is filled inside +-0.5 C, so 'this model shows no eclipse' renders as "
                "an honestly empty map instead of a noisy pastel one.",
        "draw": lambda ax, x, y, v: _anom_deadband(ax, x, y, v),
    },
    {
        "id": "b10_rdbu_clabel",
        "label": "B10. RdBu_r 1 C bands + labelled +-2/4/6 C isolines",
        "note": "B3 with inline labels on the decision-relevant contours. On an anomaly the "
                "line channel is arguably more valuable than on an absolute map.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1,
                                               cmap="RdBu_r", edges=True,
                                               iso=[-6, -4, -2, 2, 4, 6]),
    },
    {
        "id": "b11_half4",
        "label": "B11. RdBu_r, +-4 C, 0.5 C bands (scale-limit test)",
        "note": "Half the range. Doubles the contrast on the real curvature background - and "
                "saturates solid on the eclipse column, hiding exactly the magnitude in question.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_4,
                                               cmap="RdBu_r", edges=True,
                                               ticks=np.arange(-4, 4.5, 1.0)),
    },
    {
        "id": "b12_sympower",
        "label": "B12. RdBu_r +-8 C, SYMMETRIC gamma=0.40 stretch (anti-pattern)",
        "note": "The production gamma habit carried onto a diverging field. |dT| < 1 C - noise "
                "and interpolation artefacts - is expanded to fill most of the colour range.",
        "draw": lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_CONT, cmap="RdBu_r",
                                               norm=SymPowerNorm(_CLOUD_GAMMA, ANOM_HALF)),
    },
    {
        "id": "b13_twoslope",
        "label": "B13. TwoSlopeNorm, ASYMMETRIC autoscaled limits (anti-pattern)",
        "note": "TwoSlopeNorm(vmin=min, vcenter=0, vmax=max) per frame. Zero stays at the "
                "colour centre, but the two halves get different C-per-colour - so a -7 C dip "
                "and a +2 C bump can be drawn equally dark.",
        "draw": None,  # data-dependent, special-cased below
    },
]


def _draw_twoslope(ax, lons, lats, v):
    lo = float(min(np.nanmin(v), -0.5))
    hi = float(max(np.nanmax(v), 0.5))
    norm = mcolors.TwoSlopeNorm(vmin=lo, vcenter=0.0, vmax=hi)
    levels = np.concatenate([np.linspace(lo, 0, 9)[:-1], np.linspace(0, hi, 9)])
    cs = _fill(ax, lons, lats, v, levels=levels, cmap="RdBu_r", norm=norm, extend="neither")
    draw_colorbar(ax, cs, np.round(np.linspace(lo, hi, 5), 1),
                  f"anomaly (C) - TwoSlopeNorm {lo:.1f}..{hi:.1f}")


# ---------------------------------------------------------------------------
# (c) Totality-overlay colour trial
# ---------------------------------------------------------------------------

OVERLAY_TRIALS = [
    {"id": "c1_red", "label": "C1. Red (production cloud-panel colour)",
     "note": "What render_frame() draws on the Blues cloud map. On a red-hot temperature "
             "panel it is nearly invisible over the Meseta.", "color": "r", "halo": False},
    {"id": "c2_black", "label": "C2. Black (production composite colour)",
     "note": "What _render_composite_frame() draws. Reads everywhere on the absolute panel; "
             "competes with the black coastline.", "color": "k", "halo": False},
    {"id": "c3_black_halo", "label": "C3. Black with a white halo",
     "note": "Same black, 2.2 pt white stroke underneath - separates it from the coastline "
             "and survives both the darkest blue and the darkest red.", "color": "k", "halo": True},
    {"id": "c4_white", "label": "C4. White",
     "note": "Inverse approach. Disappears wherever the fill is pale - the mid-scale yellows "
             "on the absolute panel, the near-zero centre on the anomaly panel.",
     "color": "w", "halo": False},
    {"id": "c5_magenta_halo", "label": "C5. Magenta with a white halo",
     "note": "A hue neither ramp contains. Unambiguous, but it is the rain layer's hue - "
             "reusing it here would make the two layers collide in a future composite.",
     "color": "#c0007f", "halo": True},
]


# ---------------------------------------------------------------------------
# (f) Road-network styling trial
# ---------------------------------------------------------------------------
#
# The problem: roads are the main geographic reference for judging WHERE on the
# peninsula something is happening (more useful than the coastline, since the
# eclipse signal is inland), and today's basemap draws them at 0.3 pt / "0.4"
# grey / 35-80% alpha. That reads over the cloud panels' pale Blues ramp and
# disappears over a saturated RdYlBu_r / RdBu_r field.
#
# It is NOT a layering problem - draw_basemap() already puts roads above the
# field (zorder 5/6 vs the fill's 1). It is a contrast AND a sub-pixel problem:
# at frame_renderer's dpi=100 one point is 100/72 = 1.39 px, so a 0.3 pt line
# is 0.42 px wide. Antialiasing spreads it over one pixel at ~40% coverage,
# and the 0.35 alpha on the secondary tier multiplies that down to ~15%. No
# colour choice rescues a line that is only ever a seventh of a pixel of ink;
# ~0.7 pt is the floor for a crisp 1 px line at this dpi.
#
# Every row below draws the accepted C3 totality overlay (black, white halo)
# unchanged, so "can I still tell roads from the totality band" is judged in
# the same image.

_CASE_DARK = "#141414"


def _rd(color, lw, alpha=1.0, casing=None, casing_lw=0.0, casing_alpha=1.0, zorder=6):
    return {"color": color, "lw": lw, "alpha": alpha, "casing": casing,
            "casing_lw": casing_lw, "casing_alpha": casing_alpha, "zorder": zorder}


ROAD_TRIALS = [
    {
        "id": "f0_baseline",
        "label": "F0. Today's draw_basemap() defaults (control)",
        "note": "0.4 grey, 0.3 pt, alpha 0.35 secondary / 0.8 major. At dpi=100 that is a "
                "0.42 px line at 15-33% effective ink. This is the row every other row has "
                "to beat, and on the hot and umbral columns it is essentially not there.",
        "roads": BASEMAP_DEFAULT_ROADS,
    },
    {
        "id": "f1_grey_bold",
        "label": "F1. Same mid-grey, heavier and fully opaque",
        "note": "0.35 grey at 0.85 pt major / 0.6 pt secondary, alpha 1.0. Isolates the "
                "linewidth/opacity half of the fix: does 'more of the same ink' suffice? Mid "
                "grey has similar luminance to the mid-scale yellows and to dark red, so it "
                "is now visible but low-contrast exactly where the field is strongest.",
        "roads": {"secondary": _rd("0.35", 0.6, zorder=5), "major": _rd("0.35", 0.85, zorder=6)},
    },
    {
        "id": "f2_white_plain",
        "label": "F2. White, no casing",
        "note": "0.9 pt major / 0.6 pt secondary, alpha 0.95. Excellent over the dark reds and "
                "the dark umbral blue - and gone over the pale mid-scale yellows, the pale "
                "03Z blues, and (fatally) inside the anomaly panel's blank +-0.5 C deadband, "
                "which is white page.",
        "roads": {"secondary": _rd("white", 0.6, 0.95, zorder=5),
                  "major": _rd("white", 0.9, 0.95, zorder=6)},
    },
    {
        "id": "f3_black_plain",
        "label": "F3. Black, no casing",
        "note": "0.85 pt major / 0.55 pt secondary, alpha 0.9. The mirror failure of F2: it "
                "owns the pale midtones and the deadband, and sinks into the darkest red and "
                "the darkest blue - the same failure that made plain black lose the totality "
                "trial to C3. It also collides with the black coastline and the band edges.",
        "roads": {"secondary": _rd("black", 0.55, 0.9, zorder=5),
                  "major": _rd("black", 0.85, 0.9, zorder=6)},
    },
    {
        "id": "f4_black_halo",
        "label": "F4. Black with a white halo (the totality overlay's own treatment)",
        "note": "0.7 pt black core inside a 1.9 pt white stroke - i.e. C3 applied to roads. "
                "Legible everywhere, which is the point: the question this row answers is "
                "whether TWO haloed-black line families in one image are separable. Compare "
                "the roads crossing the totality band against the band limits themselves.",
        "roads": {"secondary": _rd("black", 0.5, 1.0, "white", 1.5, 0.85, zorder=5),
                  "major": _rd("black", 0.7, 1.0, "white", 1.9, 0.85, zorder=6)},
    },
    {
        "id": "f5_white_cased",
        "label": "F5. White core in a dark casing (inverse of the totality treatment)",
        "note": "0.8 pt white core inside a 1.9 pt near-black casing at 0.7 alpha - the "
                "standard cartographic road casing. The pair spans both ends of the lightness "
                "axis, so one half of it always contrasts with the fill: the casing carries it "
                "on pale yellows/blanks, the white core carries it on dark red and dark blue. "
                "Reads as a light line, the opposite polarity to C3's dark line.",
        "roads": {"secondary": _rd("white", 0.55, 1.0, _CASE_DARK, 1.45, 0.6, zorder=5),
                  "major": _rd("white", 0.8, 1.0, _CASE_DARK, 1.9, 0.7, zorder=6)},
    },
    {
        "id": "f6_white_cased_major",
        "label": "F6. F5, MAJOR HIGHWAYS ONLY",
        "note": "Identical styling, secondary tier dropped entirely. The 600 px test: at the "
                "weight needed to be visible at all, does the secondary network still add "
                "geography, or is it just texture competing with the isotherm edges?",
        "roads": {"secondary": None,
                  "major": _rd("white", 0.8, 1.0, _CASE_DARK, 1.9, 0.7, zorder=6)},
    },
    {
        "id": "f7_tiered",
        "label": "F7. Cased major + plain, thinner white secondary",
        "note": "Tiers separated by TREATMENT, not only weight: major gets the full F5 casing, "
                "secondary is a bare 0.5 pt white line at 0.7 alpha. Keeps the secondary "
                "network as background texture while making the motorway skeleton read first.",
        "roads": {"secondary": _rd("white", 0.5, 0.7, zorder=5),
                  "major": _rd("white", 0.85, 1.0, _CASE_DARK, 2.0, 0.7, zorder=6)},
    },
    {
        "id": "f8_white_cased_heavy",
        "label": "F8. F5, one weight heavier",
        "note": "1.0 pt core / 2.3 pt casing on major, 0.7/1.7 on secondary. Over-drawing test "
                "- where does a road stop being a reference line and start being a feature "
                "that hides the field it is supposed to annotate?",
        "roads": {"secondary": _rd("white", 0.7, 1.0, _CASE_DARK, 1.7, 0.6, zorder=5),
                  "major": _rd("white", 1.0, 1.0, _CASE_DARK, 2.3, 0.7, zorder=6)},
    },
    {
        "id": "f9_grey_cased",
        "label": "F9. Light-grey core in a dark casing (F5, softened)",
        "note": "Same casing, but a 0.88 grey core instead of pure white, so roads never "
                "out-brighten the colourbar card or read as data. Costs some contrast against "
                "the pale end of the absolute ramp.",
        "roads": {"secondary": _rd("0.88", 0.55, 1.0, _CASE_DARK, 1.45, 0.6, zorder=5),
                  "major": _rd("0.88", 0.8, 1.0, _CASE_DARK, 1.9, 0.7, zorder=6)},
    },
    {
        "id": "f10_cased_demoted_secondary",
        "label": "F10. F5's cased major + a DEMOTED cased secondary",
        "note": "The compromise between F5 (both tiers equal-ish) and F6 (major only): the "
                "secondary tier keeps the casing - so it never disappears over a pale fill or "
                "the blank deadband, which is what killed F7 - but is pulled down to a 0.45 pt "
                "grey core in a 1.2 pt casing at 0.45 alpha, clearly subordinate to the "
                "motorway skeleton.",
        "roads": {"secondary": _rd("0.92", 0.45, 1.0, _CASE_DARK, 1.2, 0.45, zorder=5),
                  "major": _rd("white", 0.8, 1.0, _CASE_DARK, 1.9, 0.7, zorder=6)},
    },
]


# ---------------------------------------------------------------------------
# CVD simulation (same Brettel/Vienot linear-RGB approximation as the rain page)
# ---------------------------------------------------------------------------

_CVD_MATRICES = {
    "deut": np.array([[0.625, 0.375, 0.0], [0.700, 0.300, 0.0], [0.0, 0.300, 0.700]]),
    "prot": np.array([[0.567, 0.433, 0.0], [0.558, 0.442, 0.0], [0.0, 0.242, 0.758]]),
}


def _srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.clip(c, 0, None) ** (1 / 2.4) - 0.055)


def write_cvd_variant(png_name: str, kind: str = "deut") -> str:
    img = plt.imread(EXPERIMENT_DIR / png_name)[:, :, :3]
    out = _linear_to_srgb(_srgb_to_linear(img.astype(np.float64)) @ _CVD_MATRICES[kind].T)
    dst = png_name.replace(".png", f"_{kind}.png")
    plt.imsave(EXPERIMENT_DIR / dst, np.clip(out, 0, 1))
    return f"temp_panel_experiment/{dst}"


# ---------------------------------------------------------------------------
# Cell rendering
# ---------------------------------------------------------------------------


@dataclass
class Panel:
    """One (data, title) pair a candidate row can be drawn over."""

    key: str
    label: str
    title: str
    lats: np.ndarray
    lons: np.ndarray
    values: np.ndarray
    deut: bool = False


def render_cell(panel: Panel, candidate: dict, drawer, dpi: int = 140) -> str:
    bbox = eclipse_config()["bbox"]
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    drawer(ax, panel.lons, panel.lats, panel.values)
    draw_overlays(ax, bbox, color=candidate.get("color", "k"), halo=candidate.get("halo", True),
                  roads=candidate.get("roads"))

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    title = panel.title
    if not candidate.get("title_only"):
        title = f"{title} - {candidate['label'].split('. ', 1)[-1]}"
    ax.set_title(title, fontsize=6.5)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{candidate['id']}__{panel.key}.png"
    fig.savefig(EXPERIMENT_DIR / name, dpi=dpi)
    plt.close(fig)
    return f"temp_panel_experiment/{name}"


def build_section(section_id: str, title: str, blurb: str, panels: list[Panel],
                  candidates: list[dict], specials: dict, deut_panel: str | None) -> dict:
    columns = [{"key": p.key, "label": p.label} for p in panels]
    if deut_panel:
        columns.append({"key": f"{deut_panel}__deut",
                        "label": next(p.label for p in panels if p.key == deut_panel)
                        + " (deuteranopia sim)"})
    rows = []
    for candidate in candidates:
        drawer = candidate["draw"] or specials[candidate["id"]]
        cells = {}
        for panel in panels:
            rel = render_cell(panel, candidate, drawer)
            cells[panel.key] = rel
            if deut_panel and panel.key == deut_panel:
                cells[f"{panel.key}__deut"] = write_cvd_variant(rel.rsplit("/", 1)[-1])
        log.info("  %s / %s done", section_id, candidate["id"])
        rows.append({"id": candidate["id"], "label": candidate["label"],
                     "note": candidate["note"], "cells": cells})
    return {"id": section_id, "title": title, "blurb": blurb,
            "columns": columns, "candidates": rows}


def _accepted_abs(ax, x, y, v):
    """The accepted absolute panel: RdYlBu_r, fixed 0-44 C, 2 C bands, band edges."""
    _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2, cmap="RdYlBu_r", edges=True)


def _accepted_anom(ax, x, y, v):
    """The accepted anomaly panel: RdBu_r +-8 C, 1 C bands, +-0.5 C deadband.

    The deadband matters for section (f): inside +-0.5 C nothing is filled, so
    those pixels are bare white page - the single worst background a white road
    line can have, and a case the cloud panels never present."""
    _anom_deadband(ax, x, y, v)


def build_roads_section(f: dict, dpi: int = 100) -> dict:
    """Section (f): road styling only, everything else held at the accepted design.

    Rendered at PRODUCTION dpi=100, not the dpi=140 the rest of the page uses:
    the whole question is sub-pixel line rendering, so judging it at a dpi
    production does not ship would decide the wrong thing (cf. section (d),
    where dpi=100 is what killed the clabel isotherms)."""
    glats, glons = f["glats"], f["glons"]
    run_s = _fmt_dm_z(RUN_INIT)
    valid18 = RUN_INIT + timedelta(hours=18)
    valid03 = RUN_INIT + timedelta(hours=NIGHT_STEP)

    panels = [
        Panel("abs_hot", "ABSOLUTE, hot end: GFS 18Z (dark reds)",
              f"GFS - {run_s} -> {_fmt_dm_z(valid18)} (+18h)", glats, glons, f["g18"]),
        Panel("abs_cold", "ABSOLUTE, cold end: GFS 03Z (blues + pale midtones)",
              f"GFS - {run_s} -> {_fmt_dm_z(valid03)} (+{NIGHT_STEP}h)",
              glats, glons, f["gfs"][NIGHT_STEP][2]),
        Panel("anom_cold", "ANOMALY, dark umbra (SYNTHETIC eclipse, -8 C)",
              f"HRES - blind mean + SYNTH eclipse - {run_s}, 18Z",
              glats, glons, f["hres_vs_blind"] + f["synth"]),
        Panel("anom_warm", "ANOMALY, dark red end + blank deadband (REAL curvature, +7 C)",
              f"GFS curvature - {run_s}, 18Z vs 15/21Z", glats, glons, f["curvature"]),
    ]
    deut_keys = ["abs_hot", "anom_cold"]
    columns = [{"key": p.key, "label": p.label} for p in panels]
    for key in deut_keys:
        columns.append({"key": f"{key}__deut",
                        "label": next(p.label for p in panels if p.key == key)
                        + " (deuteranopia sim)"})

    rows = []
    for trial in ROAD_TRIALS:
        cells = {}
        for panel in panels:
            drawer = _accepted_abs if panel.key.startswith("abs") else _accepted_anom
            rel = render_cell(panel, trial, drawer, dpi=dpi)
            cells[panel.key] = rel
            if panel.key in deut_keys:
                cells[f"{panel.key}__deut"] = write_cvd_variant(rel.rsplit("/", 1)[-1])
        rows.append({"id": trial["id"], "label": trial["label"],
                     "note": trial["note"], "cells": cells})
        log.info("  roads / %s done", trial["id"])

    return {
        "id": "roads",
        "title": "(f) How to draw the road network",
        "blurb": "Only the ROAD styling changes down this section - colourmap, banding, "
                 "deadband, band edges, colourbar, coastline (black, 0.5 pt) and the accepted "
                 "C3 totality overlay (black, white halo) are identical in all 36 panels, so "
                 "every row is also a 'can I still tell roads from the totality band' test. "
                 "Rendered at PRODUCTION dpi=100 (600 px wide, upscaled here), because the "
                 "failure being fixed is partly sub-pixel: at dpi=100 one point is 1.39 px, so "
                 "today's 0.3 pt road is 0.42 px of line before its alpha is even applied. The "
                 "four columns are the two extremes of each scale: dark red and pale blue on "
                 "the absolute ramp, the dark umbral blue and the +7 C dark red (plus large "
                 "areas of blank white deadband) on the anomaly ramp.",
        "columns": columns,
        "candidates": rows,
    }


# ---------------------------------------------------------------------------


def load_fields() -> dict:
    """Every field the page needs, read once from the shared local GRIB cache.

    Factored out of main() so the section-only entry points (`... roads`) can
    re-render one section without re-rendering the whole page - the fetches are
    cached, but ~190 figures are not free and the scheduler container is busy
    archiving live runs."""
    CACHE.mkdir(parents=True, exist_ok=True)
    bbox = eclipse_config()["bbox"]

    log.info("fetching GFS ...")
    gfs = {s: read_gfs(s, bbox) for s in (*STEPS, *STEPS_NEXT_DAY, NIGHT_STEP)}
    log.info("fetching ICON-EU ...")
    icon = {s: read_icon_eu(s, bbox) for s in STEPS}
    log.info("fetching ECMWF HRES ...")
    hres = {s: read_hres(s, bbox) for s in (18,)}

    glats, glons, g18 = gfs[18]
    _, _, g15 = gfs[15]
    _, _, g21 = gfs[21]

    land = land_mask_from_diurnal(g15, g21)
    synth = synth_eclipse_cooling(glats, glons, land)

    # --- the four candidate anomaly BASELINES, all on the GFS grid -----------
    # 1. naive temporal: exactly the T(18Z) - baseline(15Z,21Z) the project's
    #    15/18/21 archive hours were chosen for.
    curvature = g18 - 0.5 * (g15 + g21)
    # 2. double difference: the same curvature one day later, subtracted off.
    #    Both days are eclipse-free, so whatever survives is pure background.
    curvature_d2 = gfs[42][2] - 0.5 * (gfs[39][2] + gfs[45][2])
    double_diff = curvature - curvature_d2
    # 3. cross-model, same valid time: GFS minus ICON-EU.
    icon_on_gfs = regrid(icon[18], glats, glons)
    spread = g18 - icon_on_gfs
    # 4. the operational shape of (3): the eclipse-AWARE model minus the mean of
    #    two eclipse-blind ones, at the identical valid time.
    hres_on_gfs = regrid(hres[18], glats, glons)
    hres_vs_blind = hres_on_gfs - 0.5 * (g18 + icon_on_gfs)

    return {
        "gfs": gfs, "icon": icon, "hres": hres,
        "glats": glats, "glons": glons, "g18": g18, "g15": g15, "g21": g21,
        "land": land, "synth": synth, "curvature": curvature,
        "double_diff": double_diff, "spread": spread, "hres_vs_blind": hres_vs_blind,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    f = load_fields()
    gfs, icon, hres = f["gfs"], f["icon"], f["hres"]
    glats, glons, g18 = f["glats"], f["glons"], f["g18"]
    land, synth = f["land"], f["synth"]
    curvature, double_diff = f["curvature"], f["double_diff"]
    spread, hres_vs_blind = f["spread"], f["hres_vs_blind"]

    ilats, ilons, i18 = icon[18]
    hlats, hlons, h18 = hres[18]

    valid18 = RUN_INIT + timedelta(hours=18)
    valid03 = RUN_INIT + timedelta(hours=NIGHT_STEP)
    run_s = _fmt_dm_z(RUN_INIT)

    abs_panels = [
        Panel("gfs18", "GFS 0.25 - 18Z", f"GFS - {run_s} -> {_fmt_dm_z(valid18)} (+18h)",
              glats, glons, g18),
        Panel("icon18", "ICON-EU 0.0625 - 18Z",
              f"ICON-EU - {run_s} -> {_fmt_dm_z(valid18)} (+18h)", ilats, ilons, i18),
        Panel("hres18", "ECMWF HRES 0.25 - 18Z",
              f"ECMWF HRES - {run_s} -> {_fmt_dm_z(valid18)} (+18h)", hlats, hlons, h18),
        Panel("gfs03", "GFS - 03Z (cold end)",
              f"GFS - {run_s} -> {_fmt_dm_z(valid03)} (+{NIGHT_STEP}h)",
              glats, glons, gfs[NIGHT_STEP][2]),
    ]
    anom_panels = [
        Panel("xm", "REAL, no eclipse: HRES - mean(GFS, ICON-EU) @ 18Z",
              f"HRES - blind mean - {run_s} -> {_fmt_dm_z(valid18)}",
              glats, glons, hres_vs_blind),
        Panel("ecl_xm", "Same + SYNTHETIC eclipse in HRES",
              f"HRES - blind mean + SYNTH eclipse - {run_s}, 18Z",
              glats, glons, hres_vs_blind + synth),
        Panel("curv", "REAL: GFS T(18Z) - mean(15Z,21Z)",
              f"GFS curvature - {run_s}, 18Z vs 15/21Z", glats, glons, curvature),
    ]

    specials_abs = {"a12_autoscale": _draw_autoscale}
    specials_anom = {"b13_twoslope": _draw_twoslope}

    sections = []
    log.info("rendering absolute section ...")
    sections.append(build_section(
        "absolute", "(a) Absolute 2 m temperature",
        "Every row is the same fixed scale applied to four real fields at one valid time, "
        "except where the row's own label says otherwise. The 03Z column exists to test what "
        "a scale chosen for the eclipse hour does to the rest of the same run.",
        abs_panels, ABS_CANDIDATES, specials_abs, deut_panel="gfs18",
    ))
    log.info("rendering anomaly section ...")
    anomaly_section = build_section(
        "anomaly", "(b) Anomaly / difference, symmetric about zero",
        "Columns 1 and 3 are 100% REAL. Column 2 is column 1 plus a SYNTHETIC umbral cooling - "
        "no real eclipse forecast exists yet (2026-08-12 is beyond every model's current "
        "range), so the -7 C dip is fabricated at the documented IFS Cy50r1 magnitude to test "
        "the rendering, nothing more. Column 3 (the naive temporal baseline) is here as the "
        "hardest real stress test: it is almost entirely one-sided and reaches +7 C.",
        anom_panels, ANOM_CANDIDATES, specials_anom, deut_panel="ecl_xm",
    )

    log.info("rendering baseline section ...")
    baseline_variants = [
        ("e1_naive", "E1. T(18Z) - mean(T(15Z), T(21Z)), one model",
         "The baseline the 15/18/21 UTC archive hours were chosen to support. Its own "
         "eclipse-free background is the field on the left - and it is NOT small.",
         curvature),
        ("e2_dd", "E2. Double difference: that curvature minus the next day's",
         "Same model, same run, same three clock hours 24 h later. Removes the systematic "
         "shape of the evening cooling curve; what is left is day-to-day weather.",
         double_diff),
        ("e3_xm2", "E3. GFS - ICON-EU, identical valid time",
         "Two eclipse-blind models differenced. This is the pure inter-model spread the brief "
         "expected to swamp the signal - measured here, not assumed.",
         spread),
        ("e4_xm3", "E4. HRES - mean(GFS, ICON-EU), identical valid time",
         "The operational shape: the ONLY eclipse-simulating model minus the mean of two "
         "eclipse-blind ones. Averaging the reference halves its noise.",
         hres_vs_blind),
    ]
    baseline_rows = []
    for bid, blabel, bnote, field in baseline_variants:
        cand = {"id": bid, "label": blabel, "note": bnote, "title_only": True}
        drawer = lambda ax, x, y, v: _anom_cell(  # noqa: E731
            ax, x, y, v, levels=ANOM_LEVELS_1, cmap="RdBu_r", edges=True
        )
        cells = {
            "real": render_cell(
                Panel("real", "", f"REAL, no eclipse: {blabel.split('. ', 1)[-1]}",
                      glats, glons, field),
                cand, drawer),
            "synth": render_cell(
                Panel("synth", "", f"+ SYNTHETIC eclipse: {blabel.split('. ', 1)[-1]}",
                      glats, glons, field + synth),
                cand, drawer),
        }
        p99 = float(np.percentile(np.abs(field[land > 0.5]), 99))
        baseline_rows.append({
            "id": bid, "label": blabel,
            "note": bnote + f"  Measured background over land: 99th pct |dT| = {p99:.2f} C, "
                            f"range {field.min():.2f} .. {field.max():.2f} C.",
            "cells": cells,
        })
        log.info("  baseline / %s done (land p99 |dT| = %.2f)", bid, p99)
    sections.append({
        "id": "baseline", "title": "(b0) What should the anomaly be measured AGAINST?",
        "blurb": "All eight panels use the same recommended anomaly rendering (RdBu_r, +-8 C, "
                 "1 C bands + edges); only the DEFINITION of the anomaly changes. Left column "
                 "is real data with no eclipse anywhere in it - ideally near-blank. Right column "
                 "adds the same synthetic -7 C umbral dip, so the two columns together are a "
                 "signal-to-background test of each baseline.",
        "columns": [{"key": "real", "label": "REAL data, no eclipse (should be near-blank)"},
                    {"key": "synth", "label": "+ the same SYNTHETIC -7 C umbral dip"}],
        "candidates": baseline_rows,
    })
    sections.append(anomaly_section)

    log.info("rendering totality-overlay section ...")
    overlay_panels = [
        Panel("abs", "Recommended absolute panel",
              f"GFS - {run_s} -> {_fmt_dm_z(valid18)} (+18h)", glats, glons, g18),
        Panel("anom", "Recommended anomaly panel (synthetic)",
              f"HRES - blind mean + SYNTH eclipse - {run_s}, 18Z",
              glats, glons, hres_vs_blind + synth),
    ]
    overlay_rows = []
    for trial in OVERLAY_TRIALS:
        cells = {}
        for panel in overlay_panels:
            drawer = (
                (lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2,
                                               cmap="RdYlBu_r", edges=True))
                if panel.key == "abs"
                else (lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1,
                                                     cmap="RdBu_r", edges=True))
            )
            cells[panel.key] = render_cell(panel, trial, drawer)
        overlay_rows.append({"id": trial["id"], "label": trial["label"],
                             "note": trial["note"], "cells": cells})
        log.info("  overlay / %s done", trial["id"])
    sections.append({
        "id": "overlay", "title": "(c) How to draw the totality band/centreline",
        "blurb": "The recommended absolute and anomaly panels, with only the totality "
                 "band + centreline styling changed.",
        "columns": [{"key": p.key, "label": p.label} for p in overlay_panels],
        "candidates": overlay_rows,
    })

    # Production-resolution reality check: everything above is dpi=140 to make
    # fine differences judgeable; frame_renderer ships dpi=100 (a 600 px map).
    log.info("rendering dpi=100 reality check ...")
    dpi_rows = []
    for cand_id, label, note, panel, drawer in [
        ("d1_abs_100", "D1. Recommended absolute at production dpi=100",
         "Actual frame_renderer output size, shown upscaled.", abs_panels[0],
         lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2, cmap="RdYlBu_r",
                                       edges=True)),
        ("d2_abs_clabel_100", "D2. ... with labelled 5 C isotherms, dpi=100",
         "Does clabel still read at 600 px, or is it just texture?", abs_panels[0],
         lambda ax, x, y, v: _abs_cell(ax, x, y, v, levels=ABS_LEVELS_2, cmap="RdYlBu_r",
                                       edges=True, iso=np.arange(5, 45, 5.0))),
        ("d3_anom_100", "D3. Recommended anomaly at production dpi=100",
         "Same, on the synthetic-eclipse field.", anom_panels[1],
         lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1, cmap="RdBu_r",
                                        edges=True)),
        ("d4_anom_clabel_100", "D4. ... with labelled +-2/4/6 C isolines, dpi=100",
         "The anomaly panel's own clabel legibility test.", anom_panels[1],
         lambda ax, x, y, v: _anom_cell(ax, x, y, v, levels=ANOM_LEVELS_1, cmap="RdBu_r",
                                        edges=True, iso=[-6, -4, -2, 2, 4, 6])),
    ]:
        cand = {"id": cand_id, "label": label, "note": note}
        rel = render_cell(panel, cand, drawer, dpi=100)
        dpi_rows.append({"id": cand_id, "label": label, "note": note, "cells": {"p": rel}})
    sections.append({
        "id": "dpi", "title": "(d) Production-resolution reality check",
        "blurb": "frame_renderer.py saves at dpi=100 - a 600 px-wide map. These are rendered "
                 "at that real size and upscaled by the page; the softness is the finding.",
        "columns": [{"key": "p", "label": "dpi=100, upscaled"}],
        "candidates": dpi_rows,
    })

    log.info("rendering road-styling section ...")
    sections.append(build_roads_section(f))

    stats = {
        "gfs18": {"min": round(float(g18.min()), 1), "max": round(float(g18.max()), 1)},
        "icon18": {"min": round(float(i18.min()), 1), "max": round(float(i18.max()), 1)},
        "hres18": {"min": round(float(h18.min()), 1), "max": round(float(h18.max()), 1)},
        "gfs03": {"min": round(float(gfs[NIGHT_STEP][2].min()), 1),
                  "max": round(float(gfs[NIGHT_STEP][2].max()), 1)},
    }
    for name, field in [("e1_naive", curvature), ("e2_double_diff", double_diff),
                        ("e3_gfs_minus_icon", spread), ("e4_hres_minus_blind", hres_vs_blind)]:
        over_land = field[land > 0.5]
        stats[name] = {
            "min": round(float(field.min()), 2), "max": round(float(field.max()), 2),
            "land_mean_abs": round(float(np.abs(over_land).mean()), 2),
            "land_p99_abs": round(float(np.percentile(np.abs(over_land), 99)), 2),
        }
    stats["synthetic_eclipse"] = {
        "min": round(float(synth.min()), 2),
        "land_mean_where_deep": round(float(synth[synth < -3].mean()), 2),
    }
    log.info("stats: %s", json.dumps(stats, indent=2))

    GRID_JSON.write_text(json.dumps({
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_init": RUN_INIT.strftime("%Y-%m-%dT%H:00Z"),
        "stats": stats,
        "sections": sections,
    }, indent=2))
    log.info("wrote %s", GRID_JSON)


def main_roads() -> None:
    """Re-render ONLY section (f) and splice it into the existing grid JSON.

    The other 190 figures are unchanged by a road experiment, and the box this
    runs on is simultaneously archiving live model runs - re-rendering the whole
    page to change one section would be gratuitous."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    grid = json.loads(GRID_JSON.read_text())
    section = build_roads_section(load_fields())
    sections = [s for s in grid["sections"] if s["id"] != "roads"]
    sections.append(section)
    grid["sections"] = sections
    grid["generated_at"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    GRID_JSON.write_text(json.dumps(grid, indent=2))
    log.info("wrote %s (section (f) only)", GRID_JSON)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "roads":
        main_roads()
    else:
        main()
