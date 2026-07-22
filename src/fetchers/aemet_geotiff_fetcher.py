"""AEMET HARMONIE-AROME GeoTIFF fetcher (models.yaml: aemet_harmonie, fetch: geotiff).

AEMET's public, no-auth "descargas" endpoint
(``source.open_endpoint.url`` in models.yaml) serves a ``.tar.gz`` bundle of
GeoTIFF (and a couple of GeoJSON) rasters for the current HARMONIE-AROME run.
Confirmed T07(b)/T07(c) (see models.yaml aemet_harmonie.cloud.levels /
source.open_endpoint notes): AEMET has NO low/mid/high cloud breakdown
anywhere -- only a single blended total-cloud-cover field, "nubosidad". This
fetcher pulls out just that field's rasters and validates them; it never
attempts to build an L/M/H fetch, because that data does not exist.

Bundle contents observed live, 2026-07-22 (~12Z run, sampled ~19:02 UTC):
440 files, 48 hourly valid times (run_init+1h .. run_init+48h), 8 files per
valid time:

    down_<validISO8601>_11.tif              Temperatura (temperature)
    down_<validISO8601>_32.tif              Velocidad del viento (wind speed)
    down_<validISO8601>_61[_1HH|_3HH|_6HH].tif  Precipitacion (accum. precip, several windows)
    down_<validISO8601>_71.tif              Nubosidad (TOTAL CLOUD COVER, %) <- what we want
    down_<validISO8601>_207.tif             CAMPO tag says "press", but pixel value range
                                             (~0-0.2) doesn't look like hPa -- not investigated,
                                             not used here.
    down_<validISO8601>_228.tif             Descargas electricas (lightning, previous 3h)
    down_<validISO8601>_direcc_viento_33.geojson  Wind direction (point features)
    down_<validISO8601>_press_1.geojson     Pressure (point features)

Only the "71" (Nubosidad) rasters are extracted and written to
``raw_output_dir(model_name, run_init)``. The AEMET-internal numeric code
"71" for Nubosidad is NOT currently recorded in models.yaml (only the string
param name "nubosidad" is) -- see this module's docstring/findings if
models.yaml ever grows a place for it.

IMPORTANT (per project rules): each downloaded GeoTIFF here is a *rendered,
color-mapped* raster, not a raw single-band scientific array. Every file
carries a GDAL tag ``ESCALA`` containing AEMET's colour-ramp legend (RGBA
stops -> value bins) and a ``CAMPO``/``FECHA`` tag identifying the field and
valid time. The 4 raster bands are R/G/B/A of that rendered map, each with
close to the full 0-255 range of unique values (i.e. an anti-aliased colour
gradient, not a small palette of discrete legend colours). Any future
src/extract/ work for AEMET will need to invert that colour ramp
(nearest-colour match against the embedded ESCALA stops) to recover
approximate cloud-cover percentages -- there is no direct numeric band to
read. This fetcher only downloads/validates; it does not attempt that
decoding.

AEMET keeps latest-run-only (no historical archive) and this endpoint always
serves whatever the current run is, regardless of which run_init the caller
asks for -- see CLAUDE.md hard constraint #1: a missed run is unrecoverable.
Accordingly this fetcher archives every hourly cloud raster the bundle
currently contains, not just the eclipse-day archive_valid_hours_utc (which
this run may be nowhere near reaching yet -- aemet_harmonie's first_covering
is 2026-08-10T18Z). Coverage of the eclipse archive hours is still reported
via the returned FetchResult.steps/covering_steps(), same as every other
fetcher.
"""

from __future__ import annotations

import logging
import re
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
import rasterio

from src.fetchers.base import FetchResult, cycle_run_inits, raw_output_dir, steps_for_run
from src.fetchers.registry import register

log = logging.getLogger(__name__)

# Fallback only -- the real URL is read from model_config["source"]["open_endpoint"]["url"]
# per CLAUDE.md hard constraint #2 (models.yaml is the single source of truth for URLs).
_FALLBACK_DOWNLOAD_URL = "https://www.aemet.es/es/api-eltiempo/modelos/download/harmonie/PB"

# AEMET-internal product code for "Nubosidad" (total cloud cover, %), confirmed by
# inspecting a live bundle's GDAL tags (CAMPO=Nubosidad) 2026-07-22 -- not recorded
# in models.yaml today, see module docstring.
CLOUD_PRODUCT_CODE = "71"

REQUEST_TIMEOUT_S = 60.0

# Matches e.g. "down_2026-07-22T18:00:00+00:00_71.tif" -> valid="2026-07-22T18:00:00+00:00",
# code="71" (also matches "..._61_1HH.tif" -> code="61_1HH", filtered out by exact code match).
_FILENAME_RE = re.compile(
    r"^down_(?P<valid>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00)_(?P<code>[\w]+)\.tif$"
)


def _valid_time_from_filename(name: str) -> datetime | None:
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    return datetime.fromisoformat(m.group("valid")).astimezone(UTC)


def _infer_run_init(model_config: dict, valid_times: list[datetime]) -> datetime | None:
    """Best-effort reverse-inference of this bundle's actual run_init from the
    earliest valid time it contains: the latest of this model's cycle hours
    (models.yaml `cycles`) at or before that earliest valid time."""
    if not valid_times:
        return None
    earliest = min(valid_times)
    candidates = [
        c for c in cycle_run_inits(model_config["cycles"], now=earliest, lookback_hours=24)
        if c <= earliest
    ]
    return max(candidates) if candidates else None


@register("geotiff")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    steps = steps_for_run(model_config, run_init)
    out_dir = raw_output_dir(model_name, run_init)

    url = (
        model_config.get("source", {})
        .get("open_endpoint", {})
        .get("url", _FALLBACK_DOWNLOAD_URL)
    )

    try:
        resp = httpx.get(url, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps,
            status="error", error=f"download failed for {url}: {exc!r}",
        )

    content_type = resp.headers.get("content-type", "")
    if "tar" not in content_type and "gzip" not in content_type:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error",
            error=(
                f"unexpected content-type {content_type!r} from {url}, "
                "refusing to parse as tar.gz"
            ),
        )
    if len(resp.content) < 1024:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error",
            error=f"response body suspiciously small ({len(resp.content)} bytes) from {url}",
        )

    files_written: list[Path] = []
    valid_times_seen: list[datetime] = []

    try:
        with tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).name
                m = _FILENAME_RE.match(name)
                if not m or m.group("code") != CLOUD_PRODUCT_CODE:
                    continue
                valid_time = _valid_time_from_filename(name)
                if valid_time is None:
                    continue
                valid_times_seen.append(valid_time)

                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()

                # Sanitized, portable filename (original names contain ':', which
                # tarfile happily reports in member.name but which is invalid to
                # write directly on Windows -- we build our own name instead of
                # extracting the raw tar member path).
                out_name = f"{model_name}_nubosidad_{valid_time.strftime('%Y%m%dT%H%M%SZ')}.tif"
                out_path = out_dir / out_name
                out_path.write_bytes(data)
                files_written.append(out_path)
    except tarfile.TarError as exc:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps,
            status="error", error=f"tar extraction failed: {exc!r}",
        )

    if not files_written:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error",
            error=(
                f"no Nubosidad (product code {CLOUD_PRODUCT_CODE}) rasters found "
                f"in bundle from {url}"
            ),
        )

    # Validate every written file is actually a readable GeoTIFF (force a real
    # decode of band 1, not just a header parse).
    for path in files_written:
        try:
            with rasterio.open(path) as ds:
                if ds.count < 1:
                    return FetchResult(
                        model=model_name, run_init=run_init, steps=steps, status="error",
                        error=f"{path.name}: opened but has no raster bands",
                    )
                ds.read(1)
        except rasterio.errors.RasterioIOError as exc:
            return FetchResult(
                model=model_name, run_init=run_init, steps=steps, status="error",
                error=f"{path.name}: failed rasterio validation: {exc!r}",
            )

    inferred_run_init = _infer_run_init(model_config, valid_times_seen)
    if inferred_run_init is not None and inferred_run_init != run_init:
        log.warning(
            "aemet_harmonie: requested run_init %s does not match the run_init inferred "
            "from the downloaded bundle's earliest valid time (%s). AEMET's endpoint always "
            "serves whichever run is currently latest, regardless of what run_init is "
            "requested -- data has been filed under the requested run_init (%s) anyway. "
            "See models.yaml aemet_harmonie.source.open_endpoint notes.",
            run_init.isoformat(), inferred_run_init.isoformat(), run_init.isoformat(),
        )

    return FetchResult(
        model=model_name, run_init=run_init, steps=steps,
        status="ok", files_written=files_written,
    )
