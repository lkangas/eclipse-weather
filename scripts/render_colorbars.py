"""Render the static colorbar strips the tools show beside each map.

One image per QUANTITY, not per model or per frame: the scale is fixed by
frame_renderer (0-100 % cloud, a fixed 0-44 C temperature ramp), so a colorbar
is the same picture for every model, run and step. Rendering it once and
serving it as a static file is what lets the tools show it without adding a
per-frame cost to a ~30,000-frame archive.

EVERY colour, gamma and boundary here is imported from frame_renderer rather
than restated. That is the whole design constraint: a legend that disagrees
with the map is worse than no legend, and the two drift apart the moment they
hold their own copies of the numbers. If a ramp changes there, re-run this.

Geometry matches the frames deliberately. A frame PNG is a map with a title
strip on top; the colour bar is aligned to the MAP area only, so when the two
images are shown side by side at equal height the bar starts and ends exactly
where the map does instead of floating against the title.

    python -m scripts.render_colorbars                    # default output dir
    python -m scripts.render_colorbars --out /tmp/check   # somewhere else
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
import numpy as np

from src.config import eclipse_config
from src.viz.frame_renderer import (
    _CLOUD_GAMMA,
    _COMPOSITE_CHANNELS,
    _PROB_GAMMA,
    _TEMP_BAND_C,
    _TEMP_EMPHASIS_C,
    _TEMP_VMAX_C,
    _TEMP_VMIN_C,
    OUTPUT_DIR,
    _figure_layout,
)

DPI = 100
WIDTH_IN = 0.92          # narrow strip; the tools give it a fixed CSS width
INK = "#232320"          # same near-black the tool pages use for text
LABEL_SIZE = 7.0
TITLE_SIZE = 7.5


def _geometry() -> tuple[float, float, float]:
    """(figure height, map-area top fraction, map-area bottom fraction).

    Taken from the same _figure_layout() the frames use, so "same height as the
    map" is exact rather than eyeballed.
    """
    bbox = eclipse_config()["bbox"]
    _w, fig_h_in, axes_top = _figure_layout(bbox)
    return fig_h_in, axes_top, 0.0


def _new_fig() -> tuple[plt.Figure, float, float]:
    fig_h_in, axes_top, axes_bottom = _geometry()
    fig = plt.figure(figsize=(WIDTH_IN, fig_h_in))
    return fig, axes_top, axes_bottom


def _ramp_axes(fig, left: float, width: float, axes_top: float):
    """One vertical strip, occupying the same vertical band as the map."""
    return fig.add_axes([left, 0.035, width, axes_top - 0.045])


def _finish(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # transparent: the pages have their own background, and a white block
    # beside a map reads as part of the map.
    fig.savefig(path, dpi=DPI, transparent=True)
    plt.close(fig)
    return path


def _percent_ticks(ax, axes_top) -> None:
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_yticklabels(["0", "20", "40", "60", "80", "100"], fontsize=LABEL_SIZE, color=INK)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", length=2, pad=1.5, colors=INK)
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)
        spine.set_edgecolor(INK)


def render_cloud_composite(out_dir: Path, gamma: float, name: str, title: str) -> Path:
    """Three strips - high/mid/low - because the composite is not a 1-D ramp.

    The map lights one RGB channel per level and blends them over white, so a
    single ramp could only ever describe one of the three. Each strip is that
    channel on its own: white at 0 %, its full channel colour at 100 %, warped
    by exactly the gamma the map applies. Where levels overlap the map shows
    the product of these, which no static legend can enumerate - the strips
    say what each PURE level looks like, which is what a reader needs to
    decode a mixed pixel.
    """
    fig, axes_top, _ = _new_fig()
    n = len(_COMPOSITE_CHANNELS)
    gap = 0.05
    total_w = 0.52
    each_w = (total_w - gap * (n - 1)) / n
    grad = np.linspace(0, 1, 256).reshape(-1, 1)

    for i, (level, rgb) in enumerate(_COMPOSITE_CHANNELS):
        left = 0.06 + i * (each_w + gap)
        ax = _ramp_axes(fig, left, each_w, axes_top)
        # Position is LINEAR in percent; colour carries the gamma. That is the
        # standard colorbar contract - evenly spaced labels, warped ramp - and
        # it is what makes the stretch visible instead of hiding it.
        alpha = grad**gamma
        strip = np.ones((256, 1, 3)) * (1 - alpha[..., None]) + np.array(rgb) * alpha[..., None]
        ax.imshow(strip, origin="lower", aspect="auto", extent=(0, 1, 0, 100))
        ax.set_ylim(0, 100)
        ax.set_xticks([])
        if i == n - 1:
            _percent_ticks(ax, axes_top)
        else:
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_linewidth(0.5)
                spine.set_edgecolor(INK)
        ax.set_xlabel(level[0].upper(), fontsize=LABEL_SIZE + 0.5, color=INK, labelpad=2)

    fig.text(0.5, 0.995, f"{title} %", ha="center", va="top", fontsize=TITLE_SIZE,
             color=INK, fontweight="bold")
    return _finish(fig, out_dir / f"{name}.png")


def render_cloud_total(out_dir: Path) -> Path:
    """Single Blues ramp - the fallback field for models with no native
    low/mid/high (ecmwf_hres, ecmwf_ens, aemet_harmonie)."""
    fig, axes_top, _ = _new_fig()
    ax = _ramp_axes(fig, 0.10, 0.48, axes_top)
    norm = mcolors.PowerNorm(gamma=_CLOUD_GAMMA, vmin=0, vmax=100)
    grad = np.linspace(0, 100, 256).reshape(-1, 1)
    ax.imshow(plt.get_cmap("Blues")(norm(grad)), origin="lower", aspect="auto",
              extent=(0, 1, 0, 100))
    _percent_ticks(ax, axes_top)
    fig.text(0.5, 0.995, "Total %", ha="center", va="top", fontsize=TITLE_SIZE,
             color=INK, fontweight="bold")
    return _finish(fig, out_dir / "cloud_total.png")


def render_temp(out_dir: Path) -> Path:
    """Discrete 2 C bands, matching the contourf boundaries exactly - a smooth
    ramp would imply a precision the banded map does not have."""
    fig, axes_top, _ = _new_fig()
    ax = _ramp_axes(fig, 0.10, 0.48, axes_top)
    levels = np.arange(_TEMP_VMIN_C, _TEMP_VMAX_C + _TEMP_BAND_C, _TEMP_BAND_C)
    cmap = plt.get_cmap("RdYlBu_r", len(levels) - 1)
    norm = mcolors.BoundaryNorm(levels, cmap.N)

    for lo, hi in zip(levels[:-1], levels[1:], strict=True):
        ax.axhspan(lo, hi, color=cmap(norm(lo + _TEMP_BAND_C / 2)), linewidth=0)

    # The map draws emphasis contours at these values; the bar marks the same
    # ones so a reader can carry a line from map to scale.
    for c in _TEMP_EMPHASIS_C:
        ax.axhline(c, color="#333333", linewidth=0.6, alpha=0.8)

    ax.set_ylim(_TEMP_VMIN_C, _TEMP_VMAX_C)
    ticks = list(range(int(_TEMP_VMIN_C), int(_TEMP_VMAX_C) + 1, 4))
    ax.set_yticks(ticks)
    ax.set_yticklabels([str(t) for t in ticks], fontsize=LABEL_SIZE, color=INK)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", length=2, pad=1.5, colors=INK)
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)
        spine.set_edgecolor(INK)

    fig.text(0.5, 0.995, "Temp °C", ha="center", va="top", fontsize=TITLE_SIZE,
             color=INK, fontweight="bold")
    return _finish(fig, out_dir / "temp.png")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(OUTPUT_DIR / "_legend"))
    args = ap.parse_args()
    out = Path(args.out)

    made = [
        render_cloud_composite(out, _CLOUD_GAMMA, "cloud_composite", "Cloud"),
        render_cloud_composite(out, _PROB_GAMMA, "cloud_prob_composite", "P(cloud)"),
        render_cloud_total(out),
        render_temp(out),
    ]
    for p in made:
        print(f"  {p}  ({p.stat().st_size / 1000:.1f} kB)")


if __name__ == "__main__":
    sys.exit(main())
