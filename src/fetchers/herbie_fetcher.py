"""Herbie-based fetcher for the two `fetch: herbie` models in config/models.yaml:
gfs and gefs_extended.

Downloads the low/mid/high (and, where it lives in a separate product,
total) cloud-cover GRIB2 messages for EVERY step this run publishes (not
just eclipse-day archive hours - see TASKS.md's 2026-07-23 archiver-
consolidation note for why the narrower fetch was retired), using herbie's
idx-based byte-range subsetting (`Herbie.download(search=...)`) -- never a
full GRIB2 file.

Real-idx research behind the two search regexes below (verified 2026-07-22
against live noaa-gfs-bdp-pds / noaa-gefs-pds .idx files on AWS):

- GFS (gfs.tHHz.pgrb2.0p25.fFFF) publishes L/M/H/total TWICE per step: an
  instantaneous "<n> hour fcst:" message and a windowed "<n0>-<n> hour ave
  fcst:" message. We only want the instantaneous snapshot at the target
  valid time, so the regex requires "<digits> hour fcst:" with nothing
  ("ave") in between.
- GEFS (gefs.tHHz.pgrb2{a,b}.0p50.fFFF) never publishes an instantaneous
  L/M/H/total message at all -- only the windowed average (0-3h, 0-6h,
  6-9h, ..., 18-24h, ..., 564-570h, etc., depending on lead time). There is
  nothing to filter out, so the regex only needs to select the field/level.
  Confirmed models.yaml's own T03 note: pgrb2a carries ONLY
  TCDC:entire atmosphere (total); L/M/H lives in pgrb2b.
"""

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from herbie import Herbie

from src.fetchers.base import FetchResult, full_range_steps, raw_output_dir
from src.fetchers.registry import register

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_RETRY_BACKOFF_S = 2.0


@dataclass(frozen=True)
class _Fetch:
    """One (product, search-regex, output-suffix) download unit per step."""

    suffix: str
    product: str
    search: str


# ---------------------------------------------------------------------------
# Per-model glue: translating a models.yaml entry into herbie's own
# model/product/search vocabulary. Herbie ships built-in templates named
# "gfs" and "gefs" (herbie/models/{gfs,gefs}.py) -- neither matches our
# registry names 1:1 (our "gefs_extended" maps to herbie's "gefs" template),
# and herbie has no notion of "L/M/H vs total as separate fetches", so this
# mapping is unavoidable fetcher-specific glue. It does NOT duplicate model
# metadata that base.py/models.yaml already own: cycles, steps, publication
# lags and the AWS bucket names all still come from config/models.yaml via
# src/fetchers/base.py (full_range_steps) -- the aws_bucket assertion below
# exists only to fail loudly if models.yaml's bucket ever drifts from what
# herbie's hardcoded template URLs assume.
# ---------------------------------------------------------------------------
_MODEL_SPECS = {
    "gfs": {
        "herbie_model": "gfs",
        "member": None,
        "aws_bucket": "noaa-gfs-bdp-pds",
        "fetches": (
            _Fetch(
                suffix="cloud",
                product="pgrb2.0p25",
                search=(
                    r":(?:LCDC:low|MCDC:middle|HCDC:high) cloud layer:\d+ hour fcst:"
                    r"|:TCDC:entire atmosphere:\d+ hour fcst:"
                ),
            ),
        ),
    },
    "gefs_extended": {
        "herbie_model": "gefs",
        # First working version: control member only. See open_questions
        # for fanning out to all 30 perturbed members (p01..p30).
        "member": "c00",
        "aws_bucket": "noaa-gefs-pds",
        "fetches": (
            # pgrb2a ("atmos.5" in herbie's gefs template) carries ONLY
            # TCDC:entire atmosphere at any lead time (models.yaml T03 note).
            _Fetch(suffix="total", product="atmos.5", search=r":TCDC:entire atmosphere:"),
            # pgrb2b ("atmos.5b") carries low/middle/high TCDC, always as a
            # time-window average -- confirmed no instantaneous variant
            # exists at any lead time, so no "ave" exclusion is needed here.
            _Fetch(
                suffix="levels",
                product="atmos.5b",
                search=r":TCDC:(?:low|middle|high) cloud layer:",
            ),
        ),
    },
}


def _naive_utc(dt: datetime) -> datetime:
    """Herbie asserts `date < pandas.Timestamp.utcnow().tz_localize(None)`
    (a tz-*naive* comparison) inside Herbie._validate(). Handing it a
    tz-aware datetime raises "Cannot compare tz-naive and tz-aware
    timestamps". Everything in this project is UTC already (CLAUDE.md hard
    constraint #4), so it is always safe to just drop the tzinfo before
    constructing a Herbie object.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _output_filename(model_name: str, member: str | None, step: int, suffix: str) -> str:
    parts = [f"f{step:03d}"]
    if member:
        parts.append(member)
    parts.append(suffix)
    return "_".join(parts) + ".grib2"


def _download_one(
    *,
    herbie_model: str,
    member: str | None,
    naive_date: datetime,
    step: int,
    product: str,
    search: str,
    staging_dir: Path,
) -> Path:
    """Construct a Herbie object for one step/product and download just the
    GRIB2 messages matching `search` (never the full file). Retries a
    couple of times on transient network errors (fetch politeness /
    resilience against a dead archiver, per CLAUDE.md)."""
    herbie_kwargs = dict(
        date=naive_date,
        model=herbie_model,
        product=product,
        fxx=step,
        priority=["aws"],
        save_dir=staging_dir,
        verbose=False,
    )
    if member is not None:
        herbie_kwargs["member"] = member

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            h = Herbie(**herbie_kwargs)

            if h.grib is None:
                raise RuntimeError(
                    "no GRIB2 file found on AWS (run not yet published on this "
                    "step, or beyond this run's lead-time reach)"
                )
            if h.idx is None:
                # IMPORTANT: Herbie.download() silently falls back to a FULL
                # file download if search is set but no idx file was found.
                # We must never let that happen (hard requirement: byte-range
                # subsetting only), so treat a missing idx as a hard failure
                # for this step rather than calling download() at all.
                raise RuntimeError(
                    "no .idx file found on AWS; refusing to fall back to a "
                    "full-file download"
                )

            out_path = h.download(search=search, verbose=False, errors="raise")
            if out_path is None:
                raise RuntimeError("download() returned no file")
            out_path = Path(out_path)
            if not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(f"downloaded file missing or empty: {out_path}")
            return out_path
        except Exception as exc:  # noqa: BLE001 - retried, then reported to caller
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                log.warning(
                    "herbie fetch attempt %d/%d failed for model=%s fxx=%d product=%s: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    herbie_model,
                    step,
                    product,
                    exc,
                )
                time.sleep(_RETRY_BACKOFF_S * attempt)

    assert last_exc is not None
    raise last_exc


def _require_spec(model_name: str, model_config: dict) -> dict:
    if model_name not in _MODEL_SPECS:
        raise KeyError(
            f"herbie_fetcher does not know model '{model_name}'. "
            f"Supported: {sorted(_MODEL_SPECS)}"
        )
    spec = _MODEL_SPECS[model_name]

    expected_bucket = (model_config.get("source") or {}).get("aws_bucket")
    if expected_bucket and expected_bucket != spec["aws_bucket"]:
        raise ValueError(
            f"config/models.yaml's aws_bucket for '{model_name}' ({expected_bucket!r}) "
            f"no longer matches what this fetcher's herbie glue assumes "
            f"({spec['aws_bucket']!r}). Herbie's built-in "
            f"'{spec['herbie_model']}' template hardcodes the bucket URL -- "
            "update _MODEL_SPECS (and re-verify the search regexes against a "
            "real idx file) before trusting this fetcher again."
        )
    return spec


def _download_steps(
    *, model_name: str, spec: dict, run_init: datetime, steps: list[int], out_dir: Path,
    result: FetchResult,
) -> None:
    """Download loop: fetch every (step, product) combo in `steps` into
    `out_dir`, idempotently, tolerating per-step/product failures."""
    naive_date = _naive_utc(run_init)
    member = spec["member"]

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="herbie_staging_") as staging:
        staging_dir = Path(staging)
        for step in steps:
            for f in spec["fetches"]:
                dest_path = out_dir / _output_filename(model_name, member, step, f.suffix)

                if dest_path.exists():
                    # Idempotent: don't re-download a step/product we already have.
                    result.files_written.append(dest_path)
                    continue

                try:
                    subset_path = _download_one(
                        herbie_model=spec["herbie_model"],
                        member=member,
                        naive_date=naive_date,
                        step=step,
                        product=f.product,
                        search=f.search,
                        staging_dir=staging_dir,
                    )
                    shutil.copy2(subset_path, dest_path)
                    result.files_written.append(dest_path)
                except Exception as exc:  # noqa: BLE001 - keep going for other steps/products
                    msg = f"f{step:03d}/{f.suffix}: {exc}"
                    log.error("herbie fetch failed for %s %s: %s", model_name, msg, exc)
                    errors.append(msg)

    if errors:
        result.error = "; ".join(errors)
        if not result.files_written:
            result.status = "error"
        else:
            # Partial failure: some step/product combos succeeded. Keep
            # status "ok" (there IS usable data) but still surface `.error`
            # so callers don't have to scrape logs to notice a gap.
            result.status = "ok"
            log.warning(
                "herbie fetch for %s %s partially failed (%d/%d ok): %s",
                model_name,
                run_init.isoformat(),
                len(result.files_written),
                len(result.files_written) + len(errors),
                result.error,
            )


@register("herbie")
def fetch(model_name: str, model_config: dict, run_init: datetime) -> FetchResult:
    """Fetch cloud-cover GRIB2 subsets for every step `model_name`'s
    `run_init` publishes.

    Only gfs and gefs_extended are registered under the "herbie" fetch key
    in config/models.yaml; both are handled by this one function per
    src/fetchers/registry.py's contract.
    """
    spec = _require_spec(model_name, model_config)

    reachable = full_range_steps(model_config, run_init)
    # No eclipse valid-time targets here - each step's own natural valid
    # time, zero misalignment, keeps FetchResult.steps meaningful anyway.
    # Point-extraction (which valid times matter for the eclipse archive)
    # is a downstream concern of the extractor, not this fetcher - see
    # steps_for_run() in src/fetchers/base.py.
    steps = {
        (run_init + timedelta(hours=h)).isoformat(): (h, 0.0)
        for h in reachable
    }
    result = FetchResult(model=model_name, run_init=run_init, steps=steps)

    if not reachable:
        result.status = "not_yet_covering"
        return result

    out_dir = raw_output_dir(model_name, run_init)
    _download_steps(
        model_name=model_name, spec=spec, run_init=run_init, steps=reachable,
        out_dir=out_dir, result=result,
    )
    return result
