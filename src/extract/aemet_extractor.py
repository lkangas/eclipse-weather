"""AEMET HARMONIE-AROME cloud-total extractor (models.yaml: aemet_harmonie, fetch: geotiff).

Registered under ``@register("geotiff")`` per ``src/extract/registry.py``'s convention,
keyed on models.yaml's ``fetch:`` value (aemet_harmonie is currently the only model on
this fetch path).

------------------------------------------------------------------------------------
IMPORTANT -- lossy provenance path, unlike every other model's native GRIB/NetCDF field
------------------------------------------------------------------------------------
The GeoTIFFs `aemet_geotiff_fetcher.py` archives are RENDERED, COLOR-MAPPED RASTERS (a
4-band RGBA "picture" of AEMET's own web-viewer map layer), not raw scientific arrays.
There is no numeric cloud-% band to read directly. Each file carries a GDAL ``ESCALA``
tag with AEMET's colour-ramp legend (RGBA stop -> cloud-% bin), confirmed live
2026-07-22 against ``data/raw/aemet_harmonie/2026072212/`` (see `_parse_escala`'s
docstring for the exact tag format). To recover an approximate cloud-% value per site:

    1. Parse the real ESCALA tag into an ordered list of (lo, hi, RGB) legend stops.
    2. For each site's nearest pixel, find the nearest-Euclidean-RGB legend stop
       (JPEG compression adds a few units of per-channel noise around otherwise-solid
       colour regions -- nearest-match absorbs this).
    3. Report that stop's bin MIDPOINT as the cloud-% estimate. Midpoint is a
       reasonable unbiased point estimate of an unknown-but-bounded true value inside
       the bin, given we have no finer information than "somewhere in this legend
       band" -- the alternative (lo or hi bound) would systematically bias every
       estimate low or high.

This is inherently approximate. It is reported here with ``provenance="total_only"``
(per models.yaml's already-confirmed T07(b) finding -- AEMET has no L/M/H breakdown at
all, only a single blended total-cloud field), which already flags it as a materially
different, less precise path than every other model's native fields -- but the colour-
ramp inversion adds a *second*, independent source of imprecision (legend-bin width,
typically 10 percentage points) beyond the total/native distinction. Do not treat
aemet_harmonie's cloud_total as being on the same footing as a model that reports
total_only from a real numeric GRIB field.

--------------------------------------------------------------------------------------
Legend gap below 10% -- confirmed empirically against real data, not just inferred from
the tag
--------------------------------------------------------------------------------------
AEMET's ESCALA legend for this product only defines stops from 10% up to 100% (9 stops,
see `_parse_escala`). There is no stop for 0-10%. Real files render pixels below that
range fully (or almost fully) transparent instead of assigning them a colour -- i.e.
AEMET's web-map convention is "don't draw a layer for negligible cloud" rather than
"draw a colour for zero".

This was verified, not assumed: across 5 files from the real 2026072212 run (15Z, 17Z,
19Z, 21Z on 2026-07-22, and 06Z on 2026-07-23), the alpha channel is sharply bimodal
(~0 or ~255, JPEG noise blurring only a few units either side) and the transparent
region is NOT a fixed land/sea or domain-edge mask -- it visibly grows/shrinks/moves
hour to hour like a real meteorological field (e.g. Zaragoza and Castellon read fully
transparent at +6h, then Zaragoza reads solid deep-blue (>90% legend colour) at +7h/+9h
while Castellon stays transparent, consistent with real weather evolving, not a static
rendering artifact). We therefore treat alpha < `_ALPHA_TRANSPARENT_THRESHOLD` as "below
the lowest legend stop" and assign it the same midpoint convention via an implied
(0, 10) bin -> 5.0, rather than treating it as missing/no-data.

--------------------------------------------------------------------------------------
Not reused from src/extract/base.py: `nearest_gridpoint`
--------------------------------------------------------------------------------------
`nearest_gridpoint` operates on an xarray Dataset (`.sel(..., method="nearest")`); these
files are read as a plain raster via rasterio, not opened as xarray, so nearest-pixel
lookup here uses rasterio's own `Dataset.index(lon, lat)` instead.
"""

from __future__ import annotations

import ast
import logging
import re
from datetime import UTC, datetime

import rasterio

from src.config import DATA_RAW
from src.extract.base import PointRow, file_fetched_at, sites
from src.extract.registry import register
from src.fetchers.base import format_init_dir

log = logging.getLogger(__name__)

# Matches aemet_geotiff_fetcher.py's own output naming convention exactly:
# f"{model_name}_nubosidad_{valid_time.strftime('%Y%m%dT%H%M%SZ')}.tif"
_FILENAME_RE_TEMPLATE = r"^{model}_nubosidad_(?P<valid>\d{{8}}T\d{{6}})Z\.tif$"

# See module docstring ("Legend gap below 10%"): pixels this transparent fall below
# the legend's lowest defined stop rather than being missing data.
_ALPHA_TRANSPARENT_THRESHOLD = 128
_BELOW_LEGEND_MIDPOINT = 5.0  # midpoint of the implied (0, 10) bin
_LEGEND_CEILING_PCT = 100.0  # normalizes the legend's open-ended top stop ("90+")

Stop = tuple[float, float, tuple[int, int, int]]  # (lo_pct, hi_pct, (r, g, b))


def _parse_escala(tags: dict) -> list[Stop]:
    """Parse the ``ESCALA`` GDAL tag into an ascending list of (lo, hi, RGB) stops.

    Real tag observed 2026-07-22 (aemet_harmonie 2026072212 run, every file) via
    ``rasterio.open(path).tags()["ESCALA"]`` -- a Python-dict-*repr* STRING (not JSON:
    single quotes, no ``null``), e.g. (truncated)::

        "{'Producto': '71', 'Lista RGBA': [
            {'Valores': [90.0, ''], 'RGBA': ['12', '41', '74', '255']},
            {'Valores': [80.0, 90.0], 'RGBA': ['33', '115', '184', '255']},
            ...
            {'Valores': [10.0, 20.0], 'RGBA': ['255', '255', '255', '255']}]}"

    Parsed with ``ast.literal_eval`` (safe -- evaluates only Python literals, never
    executes code -- unlike ``eval``), since the tag is Python literal syntax, not
    valid JSON. Stops are stored descending (90+ first) with the top stop's upper
    bound as ``''`` (open-ended, "90 and above"); this normalizes that to
    `_LEGEND_CEILING_PCT` and returns the list sorted ascending by lower bound.
    """
    raw = tags.get("ESCALA")
    if not raw:
        raise ValueError("GeoTIFF has no ESCALA tag -- cannot invert its colour ramp")
    parsed = ast.literal_eval(raw)
    stops: list[Stop] = []
    for entry in parsed["Lista RGBA"]:
        lo_raw, hi_raw = entry["Valores"]
        lo = float(lo_raw) if lo_raw != "" else 0.0
        hi = float(hi_raw) if hi_raw != "" else _LEGEND_CEILING_PCT
        r, g, b, _a = (int(v) for v in entry["RGBA"])
        stops.append((lo, hi, (r, g, b)))
    stops.sort(key=lambda s: s[0])
    return stops


def _pixel_to_cloud_pct(rgba: tuple[int, int, int, int], stops: list[Stop]) -> float:
    """Classify one pixel -> an approximate cloud-% value (see module docstring).

    1. ``alpha < _ALPHA_TRANSPARENT_THRESHOLD``: pixel is below the legend's lowest
       stop (AEMET renders no colour for negligible cloud) -> `_BELOW_LEGEND_MIDPOINT`.
    2. Otherwise: nearest legend stop by minimum squared Euclidean R,G,B distance.
       Alpha is excluded from this second match -- every real legend stop is opaque
       (A=255), so alpha carries no information for distinguishing *among* stops, only
       for the transparent/opaque split already handled in step 1.

    Returns the matched (or implied) bin's midpoint.
    """
    r, g, b, a = rgba
    if a < _ALPHA_TRANSPARENT_THRESHOLD:
        return _BELOW_LEGEND_MIDPOINT
    best_lo, best_hi, _rgb = min(
        stops, key=lambda s: (s[2][0] - r) ** 2 + (s[2][1] - g) ** 2 + (s[2][2] - b) ** 2
    )
    return (best_lo + best_hi) / 2.0


@register("geotiff")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    """Extract per-site cloud_total point rows from every hourly Nubosidad GeoTIFF
    already archived for this run_init (mirrors `aemet_geotiff_fetcher.py`'s own
    "archive everything the bundle currently has" philosophy -- AEMET keeps only its
    latest run, so every valid hour a run reaches is worth capturing, not just the
    eclipse-day archive_valid_hours_utc target hours, which downstream consumers can
    still filter to).

    model_config is accepted for interface-compatibility with the extractor registry
    (every extractor has the same signature) but isn't otherwise needed here: AEMET's
    fetch already writes one file per valid time, no `steps_for_run` bookkeeping is
    required to know which files to read.
    """
    run_dir = DATA_RAW / model_name / format_init_dir(run_init)
    if not run_dir.exists():
        log.warning(
            "aemet extract: no raw dir %s for run_init %s -- nothing fetched yet?",
            run_dir, run_init.isoformat(),
        )
        return []

    filename_re = re.compile(_FILENAME_RE_TEMPLATE.format(model=re.escape(model_name)))
    site_list = sites()
    rows: list[PointRow] = []

    for path in sorted(run_dir.glob(f"{model_name}_nubosidad_*.tif")):
        m = filename_re.match(path.name)
        if not m:
            continue
        valid_time = datetime.strptime(m.group("valid"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        fetched_at = file_fetched_at(path)

        try:
            with rasterio.open(path) as ds:
                stops = _parse_escala(ds.tags())
                bands = ds.read()  # shape (4, H, W): R, G, B, A
                height, width = ds.height, ds.width
                for site in site_list:
                    row, col = ds.index(site["lon"], site["lat"])
                    if not (0 <= row < height and 0 <= col < width):
                        log.warning(
                            "aemet extract: site %s (%.3f,%.3f) falls outside %s's "
                            "raster bounds", site["name"], site["lat"], site["lon"],
                            path.name,
                        )
                        continue
                    rgba = tuple(int(v) for v in bands[:, row, col])
                    cloud_total = _pixel_to_cloud_pct(rgba, stops)
                    rows.append(
                        PointRow(
                            model=model_name,
                            run_init=run_init,
                            member=-1,  # aemet_harmonie is deterministic
                            site=site["name"],
                            valid=valid_time,
                            cloud_low=None,
                            cloud_mid=None,
                            cloud_high=None,
                            cloud_total=cloud_total,
                            provenance="total_only",
                            fetched_at=fetched_at,
                        )
                    )
        except (rasterio.errors.RasterioIOError, ValueError, KeyError) as exc:
            log.warning("aemet extract: failed to read %s: %r", path, exc)
            continue

    return rows
