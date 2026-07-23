"""Fetcher for models whose `fetch:` value in config/models.yaml is `http_bz2`:
icon_global (DWD ICON global, icosahedral) and icon_eu (DWD ICON-EU,
regular-lat-lon). Both are public, no-auth, single-file-per-step-per-param
downloads from opendata.dwd.de.

For every step this run_init publishes and each cloud param (native L/M/H +
total: CLCL/CLCM/CLCH/CLCT), this builds the download URL from the model's
`source.url_template`, GETs the .grib2.bz2 file, decompresses it, and writes
the raw .grib2 into raw_output_dir(model_name, run_init).

icon_global's grid is icosahedral (native, no regular-lat-lon variant exists
on opendata.dwd.de per T04) — this module deliberately does NOT remap it.
That cdo icosahedral -> regular-lat-lon step is a separate, more involved
pipeline stage that belongs in src/extract/ (T21), not here. This module's
only job is getting correct raw bytes onto disk for both models.

DWD's retention is only ~24h (T10) - a run must be fetched promptly after
publication_lag_h or it is gone for good (CLAUDE.md hard constraint #1).
A 404 here most often means "not published yet" or "already aged out",
not a bug - it is not retried, just recorded in FetchResult.error.
"""

from __future__ import annotations

import bz2
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from src.fetchers.base import FetchResult, full_range_steps, raw_output_dir
from src.fetchers.registry import register

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5
_POLITE_DELAY_S = 0.2  # CLAUDE.md fetch politeness: many small per-param/per-step files
_USER_AGENT = "eclipse-weather-archiver/0.1 (contact: lauri@farsight.space)"


def _cloud_params(model_config: dict) -> list[str]:
    """All cloud param names to fetch for this model: native L/M/H levels +
    total (e.g. CLCL/CLCM/CLCH/CLCT), de-duplicated, order preserved."""
    cloud = model_config["cloud"]
    params: list[str] = []
    for p in [*cloud.get("levels", {}).get("params", []), cloud.get("total", {}).get("param")]:
        if p and p not in params:
            params.append(p)
    return params


def _build_url(url_template: str, *, hh: str, yyyymmddhh: str, fff: str, param: str) -> str:
    return url_template.format(
        HH=hh, param_lower=param.lower(), YYYYMMDDHH=yyyymmddhh, FFF=fff, PARAM=param
    )


def _download_bz2(client: httpx.Client, url: str, dest_bz2: Path) -> None:
    """Stream url to dest_bz2. Raises immediately (no retry) on 404 - that is
    a permanent condition (not yet published, or DWD's ~24h retention already
    expired it), not a transient failure. Retries with backoff on transport
    errors / timeouts / non-404 HTTP errors."""
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    raise httpx.HTTPStatusError(
                        f"404 Not Found: {url}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                with open(dest_bz2, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
            return
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise
            last_exc = e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def _decompress(dest_bz2: Path, dest_grib2: Path) -> None:
    with bz2.open(dest_bz2, "rb") as src, open(dest_grib2, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)


def _download_all_params(
    *, model_name: str, model_config: dict, run_init: datetime, steps: list[int], out_dir: Path,
) -> tuple[list[Path], list[str]]:
    """Shared download loop: fetch every (step, param) combo into `out_dir`,
    idempotently."""
    url_template = model_config["source"]["url_template"]
    params = _cloud_params(model_config)
    if not params:
        raise ValueError(f"{model_name}: no cloud params found in model_config['cloud']")

    hh = run_init.strftime("%H")
    yyyymmddhh = run_init.strftime("%Y%m%d%H")

    files_written: list[Path] = []
    errors: list[str] = []

    with httpx.Client(
        timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    ) as client:
        for step in steps:
            fff = f"{step:03d}"
            for param in params:
                url = _build_url(url_template, hh=hh, yyyymmddhh=yyyymmddhh, fff=fff, param=param)
                grib2_name = Path(url).name.removesuffix(".bz2")
                dest_grib2 = out_dir / grib2_name

                if dest_grib2.exists() and dest_grib2.stat().st_size > 0:
                    files_written.append(dest_grib2)  # idempotent re-run
                    continue

                dest_bz2 = out_dir / (grib2_name + ".bz2")
                try:
                    _download_bz2(client, url, dest_bz2)
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code if e.response is not None else "?"
                    msg = f"{param} f{fff}: HTTP {code} for {url}"
                    logger.warning(msg)
                    errors.append(msg)
                    continue
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    msg = f"{param} f{fff}: {type(e).__name__}: {e}"
                    logger.warning(msg)
                    errors.append(msg)
                    continue

                try:
                    _decompress(dest_bz2, dest_grib2)
                except Exception as e:
                    msg = f"{param} f{fff}: decompress failed: {type(e).__name__}: {e}"
                    logger.warning(msg)
                    errors.append(msg)
                    dest_grib2.unlink(missing_ok=True)
                    continue
                finally:
                    dest_bz2.unlink(missing_ok=True)

                files_written.append(dest_grib2)
                time.sleep(_POLITE_DELAY_S)

    return files_written, errors


def _result_from_download(
    model_name: str, run_init: datetime, steps_map: dict, files_written: list[Path],
    errors: list[str],
) -> FetchResult:
    result = FetchResult(model=model_name, run_init=run_init, steps=steps_map)
    result.files_written = files_written
    if not files_written:
        result.status = "error"
        result.error = "; ".join(errors) if errors else "no files written, no errors recorded"
    elif errors:
        result.status = "ok"
        result.error = "partial failures: " + "; ".join(errors)
    else:
        result.status = "ok"
    return result


@register("http_bz2")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch DWD ICON cloud fields (icon_global raw icosahedral / icon_eu
    regular-lat-lon) for every step this run_init publishes. See module
    docstring for scope (no cdo remap here - T21's job)."""
    reachable = full_range_steps(model_config, run_init)
    steps_map = {
        (run_init + timedelta(hours=h)).isoformat(): (h, 0.0)
        for h in reachable
    }
    if not reachable:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps_map, status="not_yet_covering"
        )

    out_dir = raw_output_dir(model_name, run_init)
    try:
        files_written, errors = _download_all_params(
            model_name=model_name, model_config=model_config, run_init=run_init,
            steps=reachable, out_dir=out_dir,
        )
    except ValueError as e:
        return FetchResult(
            model=model_name, run_init=run_init, steps=steps_map, status="error", error=str(e)
        )

    return _result_from_download(model_name, run_init, steps_map, files_written, errors)
