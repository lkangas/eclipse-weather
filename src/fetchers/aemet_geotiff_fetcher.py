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

THREE consequences of "latest run only", all handled here rather than by the
scheduler (which is generic and must stay that way):

1. The requested run_init is NOT the run you get. The bundle's own true init
   is read from the ``name`` field every ``*_press_1.geojson`` member carries
   ("fc<YYYYMMDDHH>+NNNh00m_1", verified live 2026-07-27: all 48 members of a
   bundle agree), and the rasters are filed under THAT. The GeoTIFFs' GDAL
   tags carry only FECHA (valid time) and no reference time, so the GeoJSON
   sidecar is the only in-band statement of the run init. Reverse-inferring
   the init from the earliest valid time (init+1h) is kept as a fallback.
   That init is also what comes back in ``FetchResult.run_init``, and CALLERS
   MUST FOLLOW IT rather than the init they asked for: extracting, marking or
   reclaiming the requested run instead files work against a directory this
   fetch never wrote to (see src/scheduler/run.py's ``fetched_init``).

2. This model must not take part in the generic 48-hour top-up pass. A
   latest-only endpoint can never "gain steps" for an older run, so a top-up
   can only ever re-download the CURRENT bundle - which, before consequence 1
   was fixed, wrote the current run's rasters into an older run's directory
   and produced the contaminated archive found on 2026-07-27 (23 directories
   holding 13 distinct runs, 9 of them mixing 2-4 runs together). Because
   FETCH_TOPUP_WINDOW_H is global and load-bearing for gefs_extended, the
   opt-out lives here instead: models.yaml's
   ``source.open_endpoint.serves_latest_run_only`` makes this fetcher return
   without downloading anything unless the requested run_init IS the run the
   endpoint should currently be serving.

3. Even the one surviving request per tick would re-download ~18 MB of an
   unchanged bundle every hour. The endpoint exposes no ETag or Last-Modified,
   but it does return ``Content-Disposition: attachment;
   filename=descargas_<unixtime>.tar.gz`` where <unixtime> is the bundle's
   GENERATION time (verified 2026-07-27: 1785163351 = 14:42:31Z for the 12Z
   run, ~2h42m after init, stable across repeated requests). That token is
   recorded in the run directory as ``.bundle_token`` after a successful
   archive, so a HEAD request is enough to recognise a bundle already held and
   skip the download entirely. It is a pure optimisation: if the token ever
   became per-request the fetcher would simply always download, exactly as
   before, and correctness would still rest on consequence 1.
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

from src.config import DATA_RAW
from src.fetchers.base import (
    FetchResult,
    cycle_run_inits,
    format_init_dir,
    generate_available_steps,
    latest_available_run_init,
    raw_output_dir,
    steps_for_run,
)
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

# The GeoJSON sidecar that states the run init in band -- see module docstring
# consequence 1. Its top-level "name" reads e.g. "fc2026072712+001h00m_1".
_PRESS_GEOJSON_SUFFIX = "_press_1.geojson"
_BUNDLE_NAME_RE = re.compile(r'"name"\s*:\s*"fc(?P<init>\d{10})\+(?P<step>\d{3})h')

# Only the head of each press_1 member is read: "name" is the second key of the
# object, the features array that follows is ~100 kB per file, and 48 of those
# is 4.8 MB of JSON parsed for one string.
_GEOJSON_HEAD_BYTES = 512

# Written into a run directory once its bundle is fully archived; holds the
# Content-Disposition token of the bundle those files came from (docstring
# consequence 3). A dotfile, so raw_data_files() and the render-marker count
# ignore it.
_BUNDLE_TOKEN_MARKER = ".bundle_token"

_TOKEN_RE = re.compile(r"filename=(?P<token>[^\s;\"']+)")


def _valid_time_from_filename(name: str) -> datetime | None:
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    return datetime.fromisoformat(m.group("valid")).astimezone(UTC)


def _init_from_bundle_names(names: list[str]) -> datetime | None:
    """The run init the bundle states about itself, from its press_1 GeoJSON
    ``name`` fields. Requires the members to agree with each other: a bundle
    caught mid-regeneration (half one run, half the next) is exactly the case
    that must not be filed confidently under either.
    """
    inits = {m.group("init") for m in (_BUNDLE_NAME_RE.search(n) for n in names) if m}
    if len(inits) != 1:
        if inits:
            log.warning(
                "aemet_harmonie: bundle members disagree about their run init (%s) - "
                "falling back to valid-time inference", sorted(inits),
            )
        return None
    return datetime.strptime(inits.pop(), "%Y%m%d%H").replace(tzinfo=UTC)


def _infer_run_init(model_config: dict, valid_times: list[datetime]) -> datetime | None:
    """Fallback reverse-inference of this bundle's actual run_init from the
    earliest valid time it contains: the latest of this model's cycle hours
    (models.yaml `cycles`) at or before that earliest valid time.

    Only used when the bundle carries no usable press_1 ``name`` field. It is
    right for an intact bundle (the earliest valid time is init+1h) but says
    nothing useful about a truncated one, which is why the in-band name is
    preferred.
    """
    if not valid_times:
        return None
    earliest = min(valid_times)
    candidates = [
        c for c in cycle_run_inits(model_config["cycles"], now=earliest, lookback_hours=24)
        if c <= earliest
    ]
    return max(candidates) if candidates else None


def _expected_raster_count(model_config: dict) -> int:
    """How many hourly Nubosidad rasters a complete bundle holds, from
    models.yaml's `steps:` rather than a hardcoded 48 (hard constraint #2).
    Step 0 is excluded: AEMET does not distribute the analysis hour."""
    return len([s for s in generate_available_steps(model_config["steps"]) if s > 0])


def _bundle_token(url: str) -> str | None:
    """The bundle's identity token from a HEAD request - no body transferred.
    None on any failure: this is an optimisation, and losing it must only cost
    a download, never a fetch."""
    try:
        resp = httpx.head(url, timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("aemet_harmonie: HEAD %s failed (%r) - will download unconditionally", url, exc)
        return None
    m = _TOKEN_RE.search(resp.headers.get("content-disposition", ""))
    return m.group("token") if m else None


def _run_holding_token(model_name: str, token: str, expected_rasters: int) -> datetime | None:
    """The archived run_init whose directory already holds this exact bundle,
    or None. Requires the directory to still hold a full set of rasters, so a
    half-written or hand-pruned run is re-fetched rather than trusted."""
    model_dir = DATA_RAW / model_name
    if not model_dir.is_dir():
        return None
    for run_dir in sorted(model_dir.iterdir(), reverse=True):
        marker = run_dir / _BUNDLE_TOKEN_MARKER
        try:
            if marker.read_text(encoding="utf-8").strip() != token:
                continue
        except OSError:
            continue
        n = len(list(run_dir.glob(f"{model_name}_nubosidad_*.tif")))
        if n < expected_rasters:
            log.info(
                "aemet_harmonie: %s carries this bundle's token but holds only %d/%d "
                "rasters - re-fetching it", run_dir.name, n, expected_rasters,
            )
            return None
        try:
            return datetime.strptime(run_dir.name, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


@register("geotiff")
def fetch(
    model_name: str,
    model_config: dict,
    run_init: datetime,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> FetchResult:
    """Archive the current HARMONIE-AROME bundle under the run init it declares.

    `force` bypasses both no-download shortcuts (the latest-only guard and the
    bundle-token check) so scripts/aemet_harmonie.py can exercise the real
    download/parse/file path on demand. It does NOT bypass the filing rule:
    a forced fetch still lands under the bundle's own init, never under the
    requested one, so forcing can't contaminate anything either.
    """
    now = now or datetime.now(UTC)
    steps = steps_for_run(model_config, run_init)

    endpoint = model_config.get("source", {}).get("open_endpoint", {})
    url = endpoint.get("url", _FALLBACK_DOWNLOAD_URL)

    # Docstring consequence 2: opt this model out of the generic top-up pass.
    # The scheduler offers every run_init inside FETCH_TOPUP_WINDOW_H once an
    # hour; for a latest-only endpoint all but one of those are guaranteed to
    # return the same current bundle, so only the current one is worth a
    # request at all.
    if endpoint.get("serves_latest_run_only") and not force:
        latest = latest_available_run_init(model_config, now)
        if latest is not None and run_init != latest:
            log.debug(
                "aemet_harmonie: not fetching %s - this endpoint serves latest-run-only "
                "and the current run is %s", run_init.isoformat(), latest.isoformat(),
            )
            return FetchResult(
                model=model_name, run_init=run_init, steps=steps, status="skipped",
            )

    # Docstring consequence 3: recognise a bundle already on disk from its
    # headers alone, before transferring ~18 MB of it.
    expected_rasters = _expected_raster_count(model_config)
    if not force:
        head_token = _bundle_token(url)
        if head_token:
            held = _run_holding_token(model_name, head_token, expected_rasters)
            if held is not None:
                log.info(
                    "aemet_harmonie: %s is already archived as run %s - skipping download",
                    head_token, format_init_dir(held),
                )
                return FetchResult(
                    model=model_name, run_init=held,
                    steps=steps_for_run(model_config, held), status="ok",
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

    # The GET's own header, not the earlier HEAD's: a bundle regenerated in
    # between would otherwise be stamped with the previous run's token, and the
    # next fetch would skip a download it actually needed.
    token_match = _TOKEN_RE.search(resp.headers.get("content-disposition", ""))
    body_token = token_match.group("token") if token_match else None

    # One SEQUENTIAL pass. tarfile over a gzip stream has no random access, so
    # extractfile() on an arbitrary member re-inflates from the start of the
    # archive; walking the members in order and buffering what we need costs
    # 3.5 MB of RAM (48 rasters) against 48 re-inflations of a 17.5 MB bundle.
    cloud_rasters: list[tuple[datetime, bytes]] = []
    bundle_name_heads: list[str] = []

    try:
        with tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if name.endswith(_PRESS_GEOJSON_SUFFIX):
                    handle = tar.extractfile(member)
                    if handle is not None:
                        bundle_name_heads.append(
                            handle.read(_GEOJSON_HEAD_BYTES).decode("utf-8", "replace")
                        )
                    continue
                m = _FILENAME_RE.match(name)
                if not m or m.group("code") != CLOUD_PRODUCT_CODE:
                    continue
                valid_time = _valid_time_from_filename(name)
                handle = tar.extractfile(member)
                if valid_time is None or handle is None:
                    continue
                cloud_rasters.append((valid_time, handle.read()))
    except tarfile.TarError as exc:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps,
            status="error", error=f"tar extraction failed: {exc!r}",
        )

    if not cloud_rasters:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps, status="error",
            error=(
                f"no Nubosidad (product code {CLOUD_PRODUCT_CODE}) rasters found "
                f"in bundle from {url}"
            ),
        )

    # The whole point (docstring consequence 1): what the bundle SAYS it is
    # beats what the caller asked for. Nothing is written until this is known.
    declared = _init_from_bundle_names(bundle_name_heads)
    if declared is None:
        declared = _infer_run_init(model_config, [vt for vt, _ in cloud_rasters])
        if declared is not None:
            log.warning(
                "aemet_harmonie: bundle carries no usable press_1 run init - fell back "
                "to inferring %s from its earliest valid time", declared.isoformat(),
            )
    effective_run_init = declared or run_init
    if declared is None:
        log.error(
            "aemet_harmonie: could not establish this bundle's run init at all - "
            "filing under the REQUESTED %s, which may be wrong", run_init.isoformat(),
        )
    elif declared != run_init:
        log.info(
            "aemet_harmonie: bundle declares itself the %s run, not the requested %s "
            "(this endpoint always serves the current run) - filing under %s",
            declared.isoformat(), run_init.isoformat(), declared.isoformat(),
        )

    out_dir = raw_output_dir(model_name, effective_run_init)
    files_written: list[Path] = []
    unchanged = 0

    for valid_time, data in sorted(cloud_rasters):
        # Sanitized, portable filename (original names contain ':', which
        # tarfile happily reports in member.name but which is invalid to
        # write directly on Windows -- we build our own name instead of
        # extracting the raw tar member path).
        out_name = f"{model_name}_nubosidad_{valid_time.strftime('%Y%m%dT%H%M%SZ')}.tif"
        out_path = out_dir / out_name
        # Byte-compare before writing, so re-fetching a bundle we already hold
        # is a genuine no-op: the mtimes stay put (they are the only record of
        # which bundle a pre-existing file came from -- see
        # scripts/aemet_harmonie.py audit) and the render marker's raw-file
        # count does not move, so nothing downstream is re-triggered.
        try:
            if out_path.stat().st_size == len(data) and out_path.read_bytes() == data:
                unchanged += 1
                continue
        except OSError:
            pass
        out_path.write_bytes(data)
        files_written.append(out_path)

    # Validate every NEWLY written file is actually a readable GeoTIFF (force a
    # real decode of band 1, not just a header parse). Files that were already
    # there byte-identical passed this check when they were written.
    for path in files_written:
        try:
            with rasterio.open(path) as ds:
                if ds.count < 1:
                    return FetchResult(
                        model=model_name, run_init=effective_run_init, steps=steps,
                        status="error", error=f"{path.name}: opened but has no raster bands",
                    )
                ds.read(1)
        except rasterio.errors.RasterioIOError as exc:
            return FetchResult(
                model=model_name, run_init=effective_run_init, steps=steps, status="error",
                error=f"{path.name}: failed rasterio validation: {exc!r}",
            )

    log.info(
        "aemet_harmonie %s: %d raster(s) written, %d already held unchanged",
        format_init_dir(effective_run_init), len(files_written), unchanged,
    )

    # Rasters in this run's directory whose valid time this bundle does not
    # contain. Under the pre-2026-07-27 fetcher that is the signature of a
    # foreign run's frames left behind by a top-up (see the module docstring);
    # reported, never deleted - see scripts/aemet_harmonie.py audit.
    bundle_valid = {vt for vt, _ in cloud_rasters}
    strays = []
    for p in out_dir.glob(f"{model_name}_nubosidad_*.tif"):
        try:
            valid = datetime.strptime(p.stem.split("_")[-1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            continue  # not one of ours to have an opinion about
        if valid not in bundle_valid:
            strays.append(p)
    if strays:
        log.warning(
            "aemet_harmonie %s: %d raster(s) in this directory are not part of the run's "
            "own bundle (pre-existing contamination) - left untouched",
            format_init_dir(effective_run_init), len(strays),
        )

    # Stamped last, so a run interrupted part-way through writing is not
    # mistaken for a complete archive of this bundle on the next pass. Failing
    # to write it costs a redundant download next time and nothing else, so it
    # must never fail the fetch - a run directory the current process cannot
    # write into is normal (the archiver container writes as root; a hand-run
    # fetch from the host does not).
    if body_token and not strays and len(cloud_rasters) >= expected_rasters:
        try:
            (out_dir / _BUNDLE_TOKEN_MARKER).write_text(body_token, encoding="utf-8")
        except OSError as exc:
            log.warning(
                "aemet_harmonie %s: could not record the bundle token (%s) - the next "
                "fetch will re-download this bundle instead of skipping it",
                format_init_dir(effective_run_init), exc,
            )

    # run_init is the EFFECTIVE one, so callers render, extract and reclaim the
    # run that was actually downloaded rather than the one they asked for.
    return FetchResult(
        model=model_name, run_init=effective_run_init,
        steps=steps_for_run(model_config, effective_run_init),
        status="ok", files_written=files_written,
    )
