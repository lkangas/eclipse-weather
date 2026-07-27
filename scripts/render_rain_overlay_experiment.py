"""Design experiment (NOT production): how should a RAIN layer be drawn on
top of the two existing cloud backgrounds?

Renders a comparison grid of candidate rain overlays - all of them
`contourf`-based per the design brief, plus a couple of contour-LINE
variants for reference - over BOTH backgrounds frame_renderer.py produces:

  * "total"          - Blues pcolormesh, PowerNorm(gamma=0.40)
  * "hml_composite"  - R=high/G=mid/B=low alpha composite over white

Both backgrounds are re-drawn here with exactly the same code path as
frame_renderer.render_frame()/_render_composite_frame() (same gamma, same
colours, same basemap + totality lines, same edge-to-edge figure layout) -
copied rather than imported because render_frame() owns its own savefig and
gives no hook to draw an extra layer on top. frame_renderer.py itself is
deliberately NOT modified by this script.

Two real GFS scenes, deliberately chosen for opposite precipitation
regimes, because a rain colour scheme that only works on one of them is
useless:

  wet  - 2026-03-05 00Z +21h: an Atlantic frontal band right across Iberia,
         ~50% of the bbox above 0.5 mm/3h, peak ~15 mm/3h. Stress-tests
         "does the overlay bury the cloud field underneath".
  conv - 2026-07-26 00Z +12h: a real archived summer run - scattered
         convective cells, ~7% of the bbox wet, peak ~27 mm/6h. This is
         what eclipse day will most likely look like, and it stress-tests
         the opposite failure: "is a small isolated cell still findable".

Precipitation is not archived by any fetcher yet (models.yaml's per-model
`rain:` blocks are researched-and-confirmed but unwired, T37), so the rain
field is pulled straight from the same AWS GRIB the gfs fetcher already
uses, via herbie idx byte-range subsetting - one APCP message per scene,
cached under DATA_ROOT/cache/rain_experiment/. GFS APCP is kg/m2 == mm.
Cloud comes from data/raw/ when that (run, step) is archived, and falls
back to the same herbie byte-range path when it isn't (the wet scene is
from March, long before this project's archive starts).

Usage (inside the Docker container, GRIB deps required):
    docker cp scripts/render_rain_overlay_experiment.py \
        eclipse-scheduler:/app/scripts/
    docker exec eclipse-scheduler /app/.venv/bin/python \
        -m scripts.render_rain_overlay_experiment
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import cfgrib
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from herbie import Herbie
from matplotlib.patches import Rectangle

from src.config import DATA_RAW, DATA_ROOT, eclipse_config
from src.extract.grib_regular_extractor import _gfs_layer_datasets
from src.fetchers.base import format_init_dir
from src.viz.basemap import draw_basemap
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

EXPERIMENT_DIR = OUTPUT_DIR / "rain_overlay_experiment"
GRID_JSON = OUTPUT_DIR / "rain_overlay_grid.json"
HERBIE_CACHE = DATA_ROOT / "cache" / "rain_experiment"

# The gfs fetcher's own cloud search regex (src/fetchers/herbie_fetcher.py's
# _MODEL_SPECS) - reused verbatim so a herbie-fetched cloud file for a
# non-archived date is byte-identical in structure to an archived
# f{step:03d}_cloud.grib2 and can be handed to the same
# _gfs_layer_datasets() reader.
_GFS_CLOUD_SEARCH = (
    r":(?:LCDC:low|MCDC:middle|HCDC:high) cloud layer:\d+ hour fcst:"
    r"|:TCDC:entire atmosphere:\d+ hour fcst:"
)

# Conventional banded precipitation thresholds (mm in the accumulation
# window), not a uniform ramp: rain is overwhelmingly near-zero with a long
# thin tail, exactly the distribution that made uniform 10% cloud bands fail
# in scripts/render_contourf_experiment.py. 0.2 mm is the lowest band edge,
# so anything drier is simply not drawn at all - contourf does not fill
# below its lowest level, which is what keeps the cloud field readable over
# the ~50-95% of Iberia that is dry in any given hour.
LEVELS = [0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]
N_BANDS = len(LEVELS) - 1 + 1  # 7 interior bands + the extend="max" band


@dataclass(frozen=True)
class Scene:
    scene_id: str
    label: str
    run_init: datetime
    step: int


SCENES = [
    Scene("wet", "Frontal band (wet)", datetime(2026, 3, 5), 21),
    Scene("conv", "Summer convection (dry)", datetime(2026, 7, 26), 12),
]

BACKGROUNDS = ["total", "hml_composite"]


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


def _to_iberia(ds, var: str, bbox: dict) -> tuple:
    """NOAA 0-360 global grid -> -180..180, sorted, cropped to bbox. Same
    conversion frame_renderer._gfs_field does."""
    lons = ds.longitude.values.copy()
    lons = np.where(lons > 180, lons - 360, lons)
    order = np.argsort(lons)
    return _crop(ds.latitude.values, lons[order], ds[var].values[:, order], bbox)


def _herbie(run_init: datetime, step: int) -> Herbie:
    return Herbie(
        run_init,
        model="gfs",
        product="pgrb2.0p25",
        fxx=step,
        save_dir=str(HERBIE_CACHE),
        verbose=False,
    )


def _apcp_search(run_init: datetime, step: int) -> str:
    """The SHORTEST accumulation window ending at this step. GFS publishes
    APCP twice or more per step - a short bucket (`24-27 hour acc fcst`) and
    a run-total (`0-27 hour acc fcst`) - and only the short bucket is a
    meaningful "rain in this hour" field; the run-total would monotonically
    swamp the map at long lead times."""
    inv = _herbie(run_init, step).inventory(r":APCP:surface:")
    best: tuple[int, str] | None = None
    for s in inv["search_this"]:
        m = re.search(r":APCP:surface:(\d+)-(\d+) hour acc fcst:", s)
        if not m or int(m.group(2)) != step:
            continue
        width = int(m.group(2)) - int(m.group(1))
        if best is None or width < best[0]:
            best = (width, s)
    if best is None:
        raise RuntimeError(f"no APCP bucket ending at f{step:03d} for {run_init}")
    return best[1]


def read_rain(run_init: datetime, step: int, bbox: dict) -> tuple:
    """(lats, lons, mm, window_hours) for one GFS step, Iberia-cropped."""
    search = _apcp_search(run_init, step)
    window = int(re.search(r":(\d+)-(\d+) hour", search).group(2)) - int(
        re.search(r":(\d+)-(\d+) hour", search).group(1)
    )
    path = _herbie(run_init, step).download(search=re.escape(search), verbose=False)
    ds = cfgrib.open_datasets(str(path))[0]
    var = "tp" if "tp" in ds.data_vars else next(iter(ds.data_vars))
    lats, lons, values = _to_iberia(ds, var, bbox)
    return lats, lons, values, window


def read_cloud(run_init: datetime, step: int, bbox: dict) -> dict[str, tuple]:
    """{'total'|'low'|'mid'|'high': (lats, lons, percent)} - from data/raw/
    when this (run, step) is archived, else byte-range fetched with the
    fetcher's own search regex into the experiment cache."""
    archived = DATA_RAW / "gfs" / format_init_dir(run_init) / f"f{step:03d}_cloud.grib2"
    if archived.exists():
        path = archived
    else:
        path = _herbie(run_init, step).download(search=_GFS_CLOUD_SEARCH, verbose=False)
    return {
        layer: _to_iberia(ds, next(iter(ds.data_vars)), bbox)
        for layer, ds in _gfs_layer_datasets(path).items()
    }


# ---------------------------------------------------------------------------
# Backgrounds - same drawing code as frame_renderer, minus the savefig
# ---------------------------------------------------------------------------


def draw_total_background(ax, cloud: dict, bbox: dict) -> None:
    lats, lons, values = cloud["total"]
    norm = mcolors.PowerNorm(gamma=_CLOUD_GAMMA, vmin=0, vmax=100)
    ax.pcolormesh(lons, lats, values, cmap="Blues", norm=norm, shading="auto", rasterized=True)


def draw_hml_background(ax, cloud: dict, bbox: dict) -> None:
    lats, _, hval = cloud["high"]
    _, _, mval = cloud["mid"]
    _, _, lval = cloud["low"]
    r_alpha = np.clip(hval / 100, 0, 1) ** _CLOUD_GAMMA
    g_alpha = np.clip(mval / 100, 0, 1) ** _CLOUD_GAMMA
    b_alpha = np.clip(lval / 100, 0, 1) ** _CLOUD_GAMMA
    canvas = np.ones(r_alpha.shape + (3,))
    for alpha, color in (
        (r_alpha, np.array([1.0, 0.0, 0.0])),
        (g_alpha, np.array([0.0, 0.65, 0.0])),
        (b_alpha, np.array([0.0, 0.3, 1.0])),
    ):
        canvas = canvas * (1 - alpha[..., None]) + color * alpha[..., None]
    if lats[0] > lats[-1]:
        canvas = canvas[::-1, :, :]
    ax.imshow(
        canvas,
        extent=(bbox["lon_min"], bbox["lon_max"], bbox["lat_min"], bbox["lat_max"]),
        origin="lower",
        aspect="auto",
        interpolation="nearest",
    )


def draw_overlays(ax, background: str, bbox: dict) -> None:
    """Basemap + totality lines, in the same colours each background uses in
    frame_renderer (red on the Blues total map, black on the composite)."""
    draw_basemap(ax, bbox)
    color = "r" if background == "total" else "k"
    band_alpha, center_alpha = (0.6, 0.8) if background == "total" else (0.5, 0.7)
    ax.plot(
        _TOTALITY_BAND_LON, _TOTALITY_BAND_LAT, f"{color}-",
        linewidth=0.8, alpha=band_alpha, zorder=7,
    )
    ax.plot(
        _TOTALITY_CENTER_LON, _TOTALITY_CENTER_LAT, f"{color}--",
        linewidth=1, alpha=center_alpha, zorder=7,
    )


# ---------------------------------------------------------------------------
# Rain overlay candidates
# ---------------------------------------------------------------------------

RAIN_Z = 4  # above the cloud fill (z~1), below basemap (5/6) and totality (7)

# Sequential magenta/violet: rises in chroma AND falls in lightness, so it
# reads as an ordered ramp in greyscale too. Magenta is the one saturated
# hue at HIGH lightness that neither background produces - the composite's
# purples only ever appear dark and desaturated (high+low overlap gives
# ~(0.5,0.15,0.5)), and Blues never leaves the blue wedge at all.
MAGENTA = ["#ffc2ea", "#ff8ad8", "#f857be", "#e0219b", "#b8007e", "#8a005e", "#5c003f", "#330023"]
# Orange/red: the textbook colourblind-safe partner for a blue background,
# but a direct hue collision with the composite's own red = high cloud.
ORANGE = ["#ffe6a8", "#ffc247", "#ff9a1f", "#f56a0c", "#d63e08", "#a52208", "#71120a", "#420806"]
# Hue-free luminance ramp - increasing opacity of one near-black ink.
NEUTRAL_ALPHAS = [0.10, 0.18, 0.27, 0.37, 0.48, 0.60, 0.73, 0.88]
WASH_ALPHAS = [0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85]

HATCHES = ["", "//", "//", "///", "///", "////", "////", "////"]
STIPPLES = ["", "..", "..", "...", "...", "oo", "oo", "OO"]
# Texture reserved for the bands that actually matter for "will this ruin
# the eclipse" - nothing below 2 mm gets hatched, so light rain stays a
# pure tint and the map does not turn into wall-to-wall texture in a wet
# scene (the failure mode candidate 8 shows plainly).
HATCH_HEAVY = ["", "", "", "//", "///", "////", "/////", "/////"]

LINE_WIDTHS = [0.4, 0.55, 0.7, 0.9, 1.1, 1.4, 1.8, 2.2]

# Per-band opacity ramp: light rain (which covers most of the area most of
# the time) stays translucent so the cloud field underneath survives;
# heavy rain (rare, small, decision-relevant) goes nearly opaque and wins.
# A single flat alpha cannot do both.
BAND_ALPHAS = [0.38, 0.48, 0.58, 0.70, 0.80, 0.88, 0.93, 0.96]


def _hatch_contourf(ax, lons, lats, rain, hatches, edgecolor, lw):
    with plt.rc_context({"hatch.linewidth": lw}):
        cs = ax.contourf(
            lons, lats, rain, levels=LEVELS, colors="none",
            hatches=hatches, extend="max", zorder=RAIN_Z,
        )
        cs.set_edgecolor(edgecolor)
        cs.set_linewidth(0)
    return cs


def _c_magenta_step(ax, lons, lats, rain):
    ax.contourf(
        lons, lats, rain, levels=LEVELS, colors=MAGENTA[:N_BANDS],
        extend="max", alpha=0.78, zorder=RAIN_Z,
    )


def _c_magenta_cont(ax, lons, lats, rain):
    cmap = mcolors.LinearSegmentedColormap.from_list("mag", MAGENTA)
    levels = np.geomspace(LEVELS[0], LEVELS[-1], 40)
    ax.contourf(
        lons, lats, rain, levels=levels, cmap=cmap,
        norm=mcolors.LogNorm(vmin=LEVELS[0], vmax=LEVELS[-1]),
        extend="max", alpha=0.78, zorder=RAIN_Z,
    )


def _c_orange_step(ax, lons, lats, rain):
    ax.contourf(
        lons, lats, rain, levels=LEVELS, colors=ORANGE[:N_BANDS],
        extend="max", alpha=0.78, zorder=RAIN_Z,
    )


def _c_lines_only(ax, lons, lats, rain):
    cs = ax.contour(
        lons, lats, rain, levels=LEVELS, colors=["#c0007f"] * len(LEVELS),
        linewidths=LINE_WIDTHS[: len(LEVELS)], zorder=RAIN_Z,
    )
    cs.set_path_effects([pe.withStroke(linewidth=2.2, foreground="white", alpha=0.85)])


def _c_lines_lightfill(ax, lons, lats, rain):
    ax.contourf(
        lons, lats, rain, levels=LEVELS,
        colors=["#ff8ad8", "#f857be", "#e0219b", "#c8008a", "#a80074", "#88005e", "#5c003f"],
        extend="max", alpha=0.26, zorder=RAIN_Z,
    )
    cs = ax.contour(
        lons, lats, rain, levels=LEVELS, colors=["#a3006c"] * len(LEVELS),
        linewidths=LINE_WIDTHS[: len(LEVELS)], zorder=RAIN_Z,
    )
    cs.set_path_effects([pe.withStroke(linewidth=2.0, foreground="white", alpha=0.8)])


def _c_hatch_only(ax, lons, lats, rain):
    _hatch_contourf(ax, lons, lats, rain, HATCHES[:N_BANDS], "#111111", 0.7)


def _c_stipple(ax, lons, lats, rain):
    _hatch_contourf(ax, lons, lats, rain, STIPPLES[:N_BANDS], "#111111", 0.6)


def _c_magenta_hatch(ax, lons, lats, rain):
    ax.contourf(
        lons, lats, rain, levels=LEVELS, colors=MAGENTA[:N_BANDS],
        extend="max", alpha=0.42, zorder=RAIN_Z,
    )
    _hatch_contourf(ax, lons, lats, rain, HATCHES[:N_BANDS], "#4a0033", 0.8)


def _c_wash_hatch(ax, lons, lats, rain):
    for i in range(N_BANDS):
        ax.contourf(
            lons, lats, rain, levels=[LEVELS[i], LEVELS[-1] * 1e3],
            colors=["#ffffff"], alpha=WASH_ALPHAS[i] / N_BANDS * 1.4, zorder=RAIN_Z,
        )
    _hatch_contourf(ax, lons, lats, rain, HATCHES[:N_BANDS], "#111111", 0.8)


def _c_luminance(ax, lons, lats, rain):
    for i in range(N_BANDS):
        upper = LEVELS[i + 1] if i + 1 < len(LEVELS) else LEVELS[-1] * 1e3
        ax.contourf(
            lons, lats, rain, levels=[LEVELS[i], upper],
            colors=["#0d0d14"], alpha=NEUTRAL_ALPHAS[i], zorder=RAIN_Z,
        )


def _banded_fill(ax, lons, lats, rain, colors, alphas, hatches=None, hatch_lw=0.8):
    """One contourf call PER BAND, so each band can carry its own alpha (and
    its own hatch). A single contourf call takes one scalar alpha for the
    whole set, which is exactly the limitation candidates 1/3 run into."""
    for i in range(N_BANDS):
        upper = LEVELS[i + 1] if i + 1 < len(LEVELS) else LEVELS[-1] * 1e3
        hatch = None if hatches is None or not hatches[i] else [hatches[i]]
        # hatches= must be omitted entirely when there is none: matplotlib
        # 3.11's ContourSet.draw() iterates self.hatches unconditionally and
        # blows up on an explicit None.
        extra = {"hatches": hatch} if hatch else {}
        with plt.rc_context({"hatch.linewidth": hatch_lw}):
            cs = ax.contourf(
                lons, lats, rain, levels=[LEVELS[i], upper], colors=[colors[i]],
                alpha=alphas[i], zorder=RAIN_Z, **extra,
            )
            if hatch:
                cs.set_edgecolor("#3d0029")
                cs.set_linewidth(0)


def _band_edges(ax, lons, lats, rain, color="#6b0049", lw=0.32):
    """Thin crisp outline on every band boundary. This is the single cheapest
    way to say "this is a different KIND of thing from the cloud underneath":
    the cloud backgrounds are blocky pcolormesh/imshow pixels with no edges
    anywhere, so a smooth outlined isohyet never reads as a cloud patch even
    when the two happen to land on similar hues."""
    ax.contour(lons, lats, rain, levels=LEVELS, colors=[color], linewidths=lw, zorder=RAIN_Z + 0.1)


def _c_magenta_edged(ax, lons, lats, rain):
    _banded_fill(ax, lons, lats, rain, MAGENTA, [0.78] * N_BANDS)
    _band_edges(ax, lons, lats, rain)


def _c_magenta_ramp(ax, lons, lats, rain):
    _banded_fill(ax, lons, lats, rain, MAGENTA, BAND_ALPHAS)
    _band_edges(ax, lons, lats, rain)


def _c_magenta_ramp_hatch(ax, lons, lats, rain):
    _banded_fill(ax, lons, lats, rain, MAGENTA, BAND_ALPHAS, hatches=HATCH_HEAVY)
    _band_edges(ax, lons, lats, rain)


def _c_orange_ramp(ax, lons, lats, rain):
    _banded_fill(ax, lons, lats, rain, ORANGE, BAND_ALPHAS)
    _band_edges(ax, lons, lats, rain, color="#5c1a00")


def _c_hatch_magenta_ink(ax, lons, lats, rain):
    _hatch_contourf(ax, lons, lats, rain, HATCHES[:N_BANDS], "#c0007f", 0.9)
    _band_edges(ax, lons, lats, rain, color="#c0007f", lw=0.5)


def _swatch_solid(colors, alpha):
    return lambda i: dict(facecolor=colors[i], alpha=alpha, edgecolor="none")


def _swatch_ramp(colors, alphas, edge="#6b0049", hatches=None):
    def f(i):
        kw = dict(facecolor=colors[i], alpha=alphas[i], edgecolor=edge, linewidth=0.4)
        if hatches is not None and hatches[i]:
            kw["hatch"] = hatches[i]
        return kw

    return f


CANDIDATES: list[dict] = [
    {
        "id": "none",
        "label": "0. No overlay (baseline)",
        "draw": None,
        "swatch": None,
        "note": "The two backgrounds as frame_renderer draws them today.",
    },
    {
        "id": "magenta_step",
        "label": "1. Magenta banded fill",
        "draw": _c_magenta_step,
        "swatch": _swatch_solid(MAGENTA, 0.78),
        "note": "Discrete magenta/violet bands, contourf alpha 0.78.",
    },
    {
        "id": "magenta_cont",
        "label": "2. Magenta continuous ramp",
        "draw": _c_magenta_cont,
        "swatch": _swatch_solid(MAGENTA, 0.78),
        "note": "Same hues, 40 log-spaced levels - a smooth ramp instead of bands.",
    },
    {
        "id": "orange_step",
        "label": "3. Orange/red banded fill",
        "draw": _c_orange_step,
        "swatch": _swatch_solid(ORANGE, 0.78),
        "note": "Warm hue the Blues background never uses; collides with the composite's red.",
    },
    {
        "id": "lines_only",
        "label": "4. Contour lines only",
        "draw": _c_lines_only,
        "swatch": lambda i: dict(
            facecolor="none", edgecolor="#c0007f", linewidth=LINE_WIDTHS[i] + 0.3
        ),
        "note": "No fill at all - haloed magenta isohyets, line weight by intensity.",
    },
    {
        "id": "lines_lightfill",
        "label": "5. Lines + very light fill",
        "draw": _c_lines_lightfill,
        "swatch": lambda i: dict(
            facecolor=MAGENTA[i], alpha=0.26, edgecolor="#a3006c", linewidth=0.8
        ),
        "note": "26% magenta tint under the same haloed isohyets.",
    },
    {
        "id": "hatch_only",
        "label": "6. Hatching only (no colour)",
        "draw": _c_hatch_only,
        "swatch": lambda i: dict(
            facecolor="none", edgecolor="#111111", hatch=HATCHES[i], linewidth=0
        ),
        "note": "contourf(colors='none', hatches=...) - density encodes intensity.",
    },
    {
        "id": "stipple",
        "label": "7. Stippling only",
        "draw": _c_stipple,
        "swatch": lambda i: dict(
            facecolor="none", edgecolor="#111111", hatch=STIPPLES[i], linewidth=0
        ),
        "note": "Dot/ring hatches instead of line hatches.",
    },
    {
        "id": "magenta_hatch",
        "label": "8. Magenta fill + hatch (dual-encoded)",
        "draw": _c_magenta_hatch,
        "swatch": lambda i: dict(
            facecolor=MAGENTA[i], alpha=0.42, edgecolor="#4a0033", hatch=HATCHES[i], linewidth=0
        ),
        "note": "42% magenta tint AND hatch density - either channel alone carries the signal.",
    },
    {
        "id": "wash_hatch",
        "label": "9. White wash + black hatch",
        "draw": _c_wash_hatch,
        "swatch": lambda i: dict(
            facecolor="#ffffff", alpha=min(0.85, 0.15 + 0.1 * i),
            edgecolor="#111111", hatch=HATCHES[i], linewidth=0,
        ),
        "note": "Desaturates the background where it rains, then hatches on top - hue-free.",
    },
    {
        "id": "luminance",
        "label": "10. Luminance only (dark ink)",
        "draw": _c_luminance,
        "swatch": lambda i: dict(facecolor="#0d0d14", alpha=NEUTRAL_ALPHAS[i], edgecolor="none"),
        "note": "One near-black ink at rising opacity - no hue used at all.",
    },
    {
        "id": "magenta_edged",
        "label": "11. Magenta bands + band edges",
        "draw": _c_magenta_edged,
        "swatch": _swatch_ramp(MAGENTA, [0.78] * N_BANDS),
        "note": "Candidate 1 plus a 0.32pt outline on every isohyet.",
    },
    {
        "id": "magenta_ramp",
        "label": "12. Magenta bands, opacity ramp + edges",
        "draw": _c_magenta_ramp,
        "swatch": _swatch_ramp(MAGENTA, BAND_ALPHAS),
        "note": "Alpha rises 0.38 -> 0.96 with intensity: light rain tints, heavy rain covers.",
    },
    {
        "id": "magenta_ramp_hatch",
        "label": "13. Opacity ramp + edges + hatch >=2mm",
        "draw": _c_magenta_ramp_hatch,
        "swatch": _swatch_ramp(MAGENTA, BAND_ALPHAS, hatches=HATCH_HEAVY),
        "note": "Candidate 12 with texture added only to the decision-relevant heavy bands.",
    },
    {
        "id": "orange_ramp",
        "label": "14. Orange bands, opacity ramp + edges",
        "draw": _c_orange_ramp,
        "swatch": _swatch_ramp(ORANGE, BAND_ALPHAS, edge="#5c1a00"),
        "note": "Candidate 12's structure in the warm hue - the fair like-for-like hue test.",
    },
    {
        "id": "hatch_magenta_ink",
        "label": "15. Hatching in magenta ink + edges",
        "draw": _c_hatch_magenta_ink,
        "swatch": lambda i: dict(
            facecolor="none", edgecolor="#c0007f", hatch=HATCHES[i], linewidth=0.5
        ),
        "note": "Candidate 6's texture-only idea, but inked in a hue with contrast on dark navy.",
    },
    # Resolution reality-check. Every row above is rendered at dpi=140 to
    # make fine differences judgeable; frame_renderer.py ships dpi=100, i.e.
    # a 600px-wide map. These two rows re-render the shortlist at the real
    # production size (the page then upscales them, so they look soft - that
    # softness IS the finding). Hatch texture that reads clearly at 840px
    # essentially disappears on a 600px frame over small convective cells.
    {
        "id": "magenta_ramp_dpi100",
        "label": "12b. Candidate 12 at production dpi=100",
        "draw": _c_magenta_ramp,
        "swatch": _swatch_ramp(MAGENTA, BAND_ALPHAS),
        "dpi": 100,
        "note": "Actual frame_renderer output size (600px), shown upscaled.",
    },
    {
        "id": "magenta_ramp_hatch_dpi100",
        "label": "13b. Candidate 13 at production dpi=100",
        "draw": _c_magenta_ramp_hatch,
        "swatch": _swatch_ramp(MAGENTA, BAND_ALPHAS, hatches=HATCH_HEAVY),
        "dpi": 100,
        "note": "Same, with the hatch - compare against 12b: the texture barely survives.",
    },
]


# ---------------------------------------------------------------------------
# Legend + frame assembly
# ---------------------------------------------------------------------------


def draw_legend(ax, swatch, window_h: int) -> None:
    """Compact banded key over the Atlantic in the bottom-left corner."""
    if swatch is None:
        return
    x0, y0, w, h = 0.025, 0.055, 0.052, 0.035
    ax.add_patch(
        Rectangle(
            (x0 - 0.015, y0 - 0.032), w * N_BANDS + 0.03, h + 0.075,
            transform=ax.transAxes, facecolor="white", alpha=0.72,
            edgecolor="0.5", linewidth=0.4, zorder=8,
        )
    )
    for i in range(N_BANDS):
        kwargs = swatch(i)
        ax.add_patch(
            Rectangle(
                (x0 + i * w, y0), w, h, transform=ax.transAxes, zorder=9, **kwargs
            )
        )
    for i, level in enumerate(LEVELS):
        ax.text(
            x0 + i * w, y0 - 0.028, f"{level:g}", transform=ax.transAxes,
            fontsize=4.6, ha="center", va="bottom", color="#222", zorder=9,
        )
    ax.text(
        x0, y0 + h + 0.008, f"rain mm/{window_h}h", transform=ax.transAxes,
        fontsize=4.8, ha="left", va="bottom", color="#222", zorder=9,
    )


# Brettel/Vienot-Mollon dichromat simulation matrices, applied in LINEAR
# RGB. An approximation, not a clinical model - good enough to answer the
# only question asked of it here: "do the rain overlay and the cloud
# background collapse onto the same colour for a red-green dichromat".
_CVD_MATRICES = {
    "deut": np.array([[0.625, 0.375, 0.0], [0.700, 0.300, 0.0], [0.0, 0.300, 0.700]]),
    "prot": np.array([[0.567, 0.433, 0.0], [0.558, 0.442, 0.0], [0.0, 0.242, 0.758]]),
}


def _srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.clip(c, 0, None) ** (1 / 2.4) - 0.055)


def write_cvd_variant(png_name: str, kind: str) -> str:
    src = EXPERIMENT_DIR / png_name
    img = plt.imread(src)[:, :, :3]
    lin = _srgb_to_linear(img.astype(np.float64))
    out = _linear_to_srgb(lin @ _CVD_MATRICES[kind].T)
    dst_name = png_name.replace(".png", f"_{kind}.png")
    plt.imsave(EXPERIMENT_DIR / dst_name, np.clip(out, 0, 1))
    return f"rain_overlay_experiment/{dst_name}"


def render_cell(scene: Scene, background: str, candidate: dict, cloud, rain, window_h) -> str:
    bbox = eclipse_config()["bbox"]
    fig_width, fig_height, axes_top = _figure_layout(bbox)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    if background == "total":
        draw_total_background(ax, cloud, bbox)
    else:
        draw_hml_background(ax, cloud, bbox)

    lats, lons, mm = rain
    if candidate["draw"] is not None:
        candidate["draw"](ax, lons, lats, mm)
    draw_overlays(ax, background, bbox)
    draw_legend(ax, candidate["swatch"], window_h)

    ax.set_xlim(bbox["lon_min"], bbox["lon_max"])
    ax.set_ylim(bbox["lat_min"], bbox["lat_max"])
    ax.set_aspect(1.3)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    valid = scene.run_init + timedelta(hours=scene.step)
    bg_label = "Total (Blues)" if background == "total" else "H/M/L composite"
    ax.set_title(
        f"GFS {bg_label} · {_fmt_dm_z(scene.run_init)} → {_fmt_dm_z(valid)} "
        f"(+{scene.step}h) · {candidate['label'].split('. ', 1)[-1]}",
        fontsize=7,
    )
    fig.subplots_adjust(left=0, right=1, bottom=0, top=axes_top)

    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{scene.scene_id}_{background}_{candidate['id']}.png"
    fig.savefig(EXPERIMENT_DIR / name, dpi=candidate.get("dpi", 140))
    plt.close(fig)
    return f"rain_overlay_experiment/{name}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    bbox = eclipse_config()["bbox"]
    HERBIE_CACHE.mkdir(parents=True, exist_ok=True)

    scene_meta = []
    scene_data = {}
    for scene in SCENES:
        log.info("reading %s ...", scene.scene_id)
        rain_lats, rain_lons, mm, window = read_rain(scene.run_init, scene.step, bbox)
        cloud = read_cloud(scene.run_init, scene.step, bbox)
        scene_data[scene.scene_id] = ((rain_lats, rain_lons, mm), cloud, window)
        wet_pct = float((mm >= LEVELS[0]).mean() * 100)
        scene_meta.append(
            {
                "id": scene.scene_id,
                "label": scene.label,
                "run_init": scene.run_init.strftime("%Y-%m-%dT%H:00Z"),
                "step": scene.step,
                "valid": (scene.run_init + timedelta(hours=scene.step)).strftime(
                    "%Y-%m-%dT%H:00Z"
                ),
                "window_h": window,
                "max_mm": round(float(np.nanmax(mm)), 2),
                "wet_area_pct": round(wet_pct, 1),
                "archived": (
                    DATA_RAW / "gfs" / format_init_dir(scene.run_init)
                    / f"f{scene.step:03d}_cloud.grib2"
                ).exists(),
            }
        )
        log.info(
            "  %s: max %.1f mm/%dh, %.1f%% of bbox >= %.1f mm",
            scene.scene_id, np.nanmax(mm), window, wet_pct, LEVELS[0],
        )

    rows = []
    for candidate in CANDIDATES:
        cells = {}
        for scene in SCENES:
            rain, cloud, window = scene_data[scene.scene_id]
            for background in BACKGROUNDS:
                key = f"{scene.scene_id}__{background}"
                rel = render_cell(scene, background, candidate, cloud, rain, window)
                cells[key] = rel
                if scene.scene_id == "conv":
                    # Dichromat check only on the realistic convective scene -
                    # doubling every column would make the page unreadable,
                    # and the wet scene's answer is the same but noisier.
                    cells[f"{key}__deut"] = write_cvd_variant(rel.rsplit("/", 1)[-1], "deut")
                log.info("  rendered %s / %s", candidate["id"], key)
        rows.append(
            {
                "id": candidate["id"],
                "label": candidate["label"],
                "note": candidate["note"],
                "cells": cells,
            }
        )

    GRID_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "levels_mm": LEVELS,
                "scenes": scene_meta,
                "backgrounds": BACKGROUNDS,
                "columns": [
                    {"key": "wet__total", "label": "WET · Blues total"},
                    {"key": "wet__hml_composite", "label": "WET · H/M/L composite"},
                    {"key": "conv__total", "label": "CONV · Blues total"},
                    {"key": "conv__hml_composite", "label": "CONV · H/M/L composite"},
                    {"key": "conv__total__deut", "label": "CONV · Blues (deuteranopia sim)"},
                    {
                        "key": "conv__hml_composite__deut",
                        "label": "CONV · H/M/L (deuteranopia sim)",
                    },
                ],
                "candidates": rows,
            },
            indent=2,
        )
    )
    log.info("wrote %s (%d candidates x %d cells)", GRID_JSON, len(rows), len(rows[0]["cells"]))


if __name__ == "__main__":
    main()
