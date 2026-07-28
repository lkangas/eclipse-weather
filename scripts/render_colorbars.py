"""Render the static colorbar strips the tools show beside each map.

One image per QUANTITY, not per model or per frame: the scale is fixed by
frame_renderer (0-100 % cloud, a fixed 0-44 C temperature ramp), so a colorbar
is the same picture for every model, run and step. Rendering it once and
serving it as a static file is what lets the tools show it without adding a
per-frame cost to a ~30,000-frame archive.

EVERY colour and boundary here is imported from frame_renderer rather
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
    _COMPOSITE_CHANNELS,
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


def render_cloud_levels(out_dir: Path) -> Path:
    """One bar, three solid blocks: which HUE means which cloud LEVEL.

    Not a magnitude scale, deliberately. The composite has no single magnitude
    axis - it lights one RGB channel per level and blends them over white, so
    three gradient strips were needed to describe it and still could not
    describe a blended pixel. The question a reader actually has in front of
    this map is "what does the green mean", and that is categorical.

    It also collapses the cloud and probability legends into one image: the
    gradients differed only by their gamma stretch (0.40 vs 0.60), and with no
    gradient there is nothing left to differ.

    Colours come from _COMPOSITE_CHANNELS, which is what the renderer blends,
    so the legend cannot drift from the map.
    """
    fig, axes_top, _ = _new_fig()
    ax = _ramp_axes(fig, 0.12, 0.40, axes_top)

    # _COMPOSITE_CHANNELS is high -> mid -> low; the bar reads bottom-up as
    # low -> mid -> high, matching the physical stacking of the atmosphere.
    for i, (level, rgb) in enumerate(reversed(_COMPOSITE_CHANNELS)):
        ax.axhspan(i, i + 1, color=rgb, linewidth=0)
        ax.text(1.35, i + 0.5, level.capitalize(), transform=ax.get_yaxis_transform(),
                ha="left", va="center", fontsize=LABEL_SIZE + 0.5, color=INK)

    ax.set_ylim(0, len(_COMPOSITE_CHANNELS))
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(0.5)
        spine.set_edgecolor(INK)

    fig.text(0.5, 0.995, "Cloud", ha="center", va="top", fontsize=TITLE_SIZE,
             color=INK, fontweight="bold")
    return _finish(fig, out_dir / "cloud_levels.png")


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

    # Two bars only. `total` gets none - a plain Blues ramp on a field that is
    # just "cloud, 0-100 %" tells a reader nothing they cannot see, and one
    # legend per quantity is a cost to keep in sync forever.
    made = [
        render_cloud_levels(out),
        render_temp(out),
    ]
    for p in made:
        print(f"  {p}  ({p.stat().st_size / 1000:.1f} kB)")


if __name__ == "__main__":
    sys.exit(main())
