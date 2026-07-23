"""Fetcher for Meteo-France ARPEGE (arpege_europe) and AROME (arome_france),
registered under models.yaml's `fetch: http_grib`.

URL discovery (2026-07-22)
---------------------------
models.yaml's `source.alt_no_auth.url` for both models points only at a
data.gouv.fr *dataset landing page* (e.g.
https://www.data.gouv.fr/datasets/paquets-arpege-resolution-0-1deg), not a
directly-downloadable file. That page's status is `verify` and its
"automation terms unconfirmed" note is about that landing page, not about a
concrete download endpoint.

The real, unauthenticated, currently-live download endpoint was pinned down
two independent ways, cross-checked live on 2026-07-22:

1. The community client github.com/CyrilJl/MeteoFetch
   (meteofetch/meteofrance/__init__.py, MeteoFrance.base_url_) downloads
   directly from an OVH-hosted S3-compatible object store operated by
   Meteo-France:

       https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/...

2. Each data.gouv.fr "resource" download link
   (https://www.data.gouv.fr/api/1/datasets/r/<resource-id>) is itself just a
   302 redirect to that exact same OVH bucket URL, confirmed by following
   several such redirects live:

       $ curl -sI https://www.data.gouv.fr/api/1/datasets/r/f6381f71-...
       location: https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/2026-07-22T12:00:00Z/arpege/01/HP1/arpege__01__HP1__000H012H__2026-07-22T12:00:00Z.grib2

So data.gouv.fr's own UI is a thin proxy in front of the same bucket this
fetcher talks to directly, avoiding the "one resource ID per package/group,
always latest run" limitation of the data.gouv.fr redirect links (the
archiver needs to address a *specific* run_init, not "whatever is latest").

Live-verified (HEAD + partial GET, 2026-07-22, real magic-byte check):
  - ARPEGE 0.1deg SP2, runs 2026-07-22T06Z and T12Z, group 000H012H: HTTP 200,
    ~110 MB, content starts with the real GRIB2 magic bytes (b"GRIB", edition 2).
  - AROME 0.025deg SP2, runs 2026-07-22T00Z/03Z/06Z, group 00H06H: HTTP 200.
    Later same-day cycles (09Z/12Z/15Z/18Z) 404'd at test time — most likely
    normal progressive-publication lag/timing for this specific real-world
    day rather than a URL-pattern problem (the *pattern* is proven by the
    00Z/03Z/06Z/yesterday-18Z/21Z successes); a fetch attempted too early for
    a given run correctly surfaces as this module's "not_yet_covering"/error
    path and the scheduler is expected to retry later, same as any other
    fetcher in this project.

TODO (flagged, not done here per task scope): back-port this real endpoint
into config/models.yaml's arpege_europe/arome_france `source.alt_no_auth`
block (that file is off-limits to this change). Until then this module is
the source of truth for the actual request shape.

Package/group layout (from MeteoFetch, itself reflecting Meteo-France's own
product structure): each paquet/run is split into several GRIB2 files, one
per fixed lead-time "group" window (e.g. ARPEGE: 000H012H, 013H024H, ...;
AROME: 00H06H, 07H12H, ...). A single file bundles every step inside its
window, so fetching a run's full range means downloading every group window
- there is no per-step file to range-fetch (unlike GFS/GEFS idx-based byte
ranges).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from src.fetchers.base import FetchResult, raw_output_dir
from src.fetchers.registry import register

logger = logging.getLogger(__name__)

BASE_URL = "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt"

# Fetcher-local knowledge of Meteo-France's package/group file layout for the
# two models this module handles. Not duplicated from models.yaml (models.yaml
# has no field for this - see module docstring); cycles/steps/lags/package
# name are still read from model_config, never hardcoded here.
_MODEL_SPECS = {
    "arpege_europe": {
        "product": "arpege",
        "resolution": "01",
        "groups": [
            "000H012H", "013H024H", "025H036H", "037H048H", "049H060H",
            "061H072H", "073H084H", "085H096H", "097H102H",
        ],
    },
    "arome_france": {
        "product": "arome",
        "resolution": "0025",
        "groups": [
            "00H06H", "07H12H", "13H18H", "19H24H", "25H30H",
            "31H36H", "37H42H", "43H48H", "49H51H",
        ],
    },
}

_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 2.0


def _run_iso(run_init: datetime) -> str:
    """Meteo-France's date token, e.g. '2026-07-22T12:00:00Z'."""
    return run_init.strftime("%Y-%m-%dT%H:00:00Z")


def _build_url(spec: dict, run_init: datetime, paquet: str, group: str) -> str:
    run_iso = _run_iso(run_init)
    filename = f"{spec['product']}__{spec['resolution']}__{paquet}__{group}__{run_iso}.grib2"
    return f"{BASE_URL}/{run_iso}/{spec['product']}/{spec['resolution']}/{paquet}/{filename}"


def _download(client: httpx.Client, url: str, dest_path) -> tuple[bool, str | None]:
    """Download url -> dest_path. Returns (ok, error_message).
    404 is treated as a normal 'not published yet' outcome, not retried."""
    last_error = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    return False, f"404 (not yet published): {url}"
                resp.raise_for_status()
                tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                tmp_path.replace(dest_path)
            return True, None
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code} for {url}"
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e} for {url}"

        if attempt < _MAX_ATTEMPTS:
            sleep_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
            logger.warning(
                "Download attempt %d/%d failed for %s (%s) - retrying in %.1fs",
                attempt, _MAX_ATTEMPTS, url, last_error, sleep_s,
            )
            time.sleep(sleep_s)
    return False, last_error


def _download_groups(
    *, model_name: str, spec: dict, run_init: datetime, groups: list[str], paquet: str,
    out_dir,
) -> tuple[list, list[str]]:
    """Download loop: fetch each named group window into `out_dir`,
    idempotently."""
    files_written = []
    errors: list[str] = []
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
        for group in groups:
            dest = out_dir / f"{model_name}_{paquet}_{group}.grib2"
            if dest.exists() and dest.stat().st_size > 0:
                files_written.append(dest)
                continue
            ok, err = _download(client, _build_url(spec, run_init, paquet, group), dest)
            if ok:
                files_written.append(dest)
            else:
                errors.append(f"{group}: {err}")
    return files_written, errors


@register("http_grib")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch every group window this run publishes. The AROME/ARPEGE group
    layout already partitions the model's ENTIRE forecast horizon into
    fixed windows, so this is simply every known group - no per-step
    computation needed like herbie_fetcher's GFS path."""
    spec = _MODEL_SPECS.get(model_name)
    if spec is None:
        return FetchResult(
            model=model_name, run_init=run_init, steps={}, status="error",
            error=f"meteofrance_fetcher has no URL spec for model '{model_name}' "
                  f"(only arpege_europe/arome_france are supported)",
        )

    paquet = model_config.get("cloud", {}).get("levels", {}).get("package", "SP2")
    out_dir = raw_output_dir(model_name, run_init)

    files_written, errors = _download_groups(
        model_name=model_name, spec=spec, run_init=run_init, groups=spec["groups"],
        paquet=paquet, out_dir=out_dir,
    )

    if not files_written:
        return FetchResult(
            model=model_name, run_init=run_init, steps={}, status="error",
            error="; ".join(errors) if errors else "no groups downloaded",
        )

    return FetchResult(
        model=model_name, run_init=run_init, steps={},
        files_written=files_written,
        status="ok" if not errors else "error",
        error="; ".join(errors) if errors else None,
    )
