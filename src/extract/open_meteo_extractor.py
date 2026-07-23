"""Open-Meteo JSON point extractor.

Registered fetch key: ``open_meteo_json`` (see ``config/models.yaml``'s
``models.ukmo_global.fetch``, and since T12, the same key on
``models.gem_global``/``models.jma_gsm``/``models.cma_grapes_global`` — all
four are primary-path-via-Open-Meteo models with no other native fetcher in
this project, so they share this one extractor). Reads the raw
``forecast.json`` saved by ``src/fetchers/open_meteo_fetcher.py`` (a JSON
list, one object per site, in ``config/sites.yaml``'s site order — confirmed
against the real archived file at
``data/raw/ukmo_global/<initYYYYMMDDHH>/forecast.json``) and emits a
``PointRow`` for every (site, target valid time) pair actually present.

Open-Meteo's hourly payload already carries real clock timestamps (not
forecast-hour offsets), so this module matches ``hourly.time`` strings
directly against ``target_valid_times(...)`` rather than reasoning about
steps via ``src.fetchers.base.steps_for_run`` — there is no step arithmetic
to reproduce here.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from src.config import DATA_RAW, eclipse_config
from src.extract.base import PointRow, file_fetched_at, sites
from src.extract.registry import register

logger = logging.getLogger(__name__)

# NOTE on NOT importing src.fetchers.base here:
# ------------------------------------------------------------------------
# src/fetchers/base.py's format_init_dir()/target_valid_times() are the
# canonical versions of the two helpers below, and would normally be the
# right thing to import. But `import src.fetchers.base` (or `from
# src.fetchers.base import ...`) forces Python to first run
# src/fetchers/__init__.py, which eagerly imports herbie_fetcher -> cfgrib
# -> eccodes -- and this dev box has no working native ecCodes install
# (confirmed repeatedly in this project's history; see this task's brief),
# so that import chain hard-crashes with RuntimeError before this module
# ever gets a chance to run. base.py itself has no such dependency (it only
# imports from src.config), so the two small helpers below are reimplemented
# locally to stay import-safe on plain Windows Python, which this specific
# extractor is required to support (pure JSON parsing, no GRIB/cfgrib
# needed). Neither helper carries any per-model registry data (cycles,
# lengths, steps, URLs, params, lags) -- CLAUDE.md hard constraint #2 is
# about not duplicating THAT, not generic date-formatting utilities -- so
# keeping these two in sync by hand with src/fetchers/base.py is low risk.
# ------------------------------------------------------------------------

HOURLY_VALID_TIME_FMT = "%Y-%m-%dT%H:%M"

# ---------------------------------------------------------------------------
# PROVENANCE per model_name, per config/models.yaml's models.open_meteo.
# cloud_provenance table (T08, extended by T12). Covers the live primary-path
# models (ukmo_global, gem_global, jma_gsm, cma_grapes_global) and T16's
# backfill-only models (gfs_global, ecmwf_ifs025, om_icon_global, om_icon_eu,
# meteofrance_arpege_europe - see scripts/backfill_open_meteo.py for why two
# of these are "om_"-prefixed: Open-Meteo's own model ids for ICON happen to
# collide with this registry's own native icon_global/icon_eu model names,
# which are a DIFFERENT pipeline/provenance - renamed to avoid silently
# conflating the two in points.parquet). gem_global/jma_gsm/cma_grapes_global
# need no such prefix - this project has no other native fetch path for them,
# so there's nothing to collide with.
#
# ukmo_global and cma_grapes_global are genuine open questions, not settled
# facts: config/models.yaml's models.ukmo_global.cloud.levels.
# provenance_via_open_meteo and models.cma_grapes_global.cloud.levels.
# provenance_via_open_meteo are both `verify`, not `confirmed` (Open-Meteo's
# docs give no explicit native-vs-derived statement for either, unlike
# gem_global/jma_gsm - T12 found explicit doc language for those two: GEM's
# low/mid/high is transitively humidity-derived, JMA's is not). "native" is
# the closest schema fit to observed non-null/varying values for the two
# `verify` models, not a documentation proof - do not let a future refactor
# treat it as settled. See each model's own models.yaml entry for what would
# actually resolve it. (cma_grapes_global also carries a much bigger caveat
# unrelated to provenance - see its reliability_caveat in models.yaml - the
# model's real-world forecast horizon fell well short of its documented
# 240h/10-day design when live-tested T12/2026-07-23.)
# ---------------------------------------------------------------------------
_PROVENANCE_BY_MODEL = {
    "ukmo_global": "native",  # verify, not confirmed - see docstring above
    "gfs_global": "native",
    "ecmwf_ifs025": "derived",
    "om_icon_global": "native",
    "om_icon_eu": "native",
    "meteofrance_arpege_europe": "native",
    "gem_global": "derived",  # T12: doc-confirmed - see gem_global.cloud.levels.note, models.yaml
    "jma_gsm": "native",  # T12: doc-confirmed - see jma_gsm.cloud.levels.note, models.yaml
    "cma_grapes_global": "native",  # verify, not confirmed - see docstring above
}
_UNRESOLVED_PROVENANCE_MODELS = {"ukmo_global", "cma_grapes_global"}
_UNRESOLVED_PROVENANCE_WARNING = (
    "%s cloud L/M/H provenance is UNRESOLVED "
    "(config/models.yaml: models.%s.cloud.levels.provenance_via_open_meteo="
    "'verify'). Writing provenance=%r as the closest schema fit to observed "
    "non-null/varying Open-Meteo values -- this is NOT a confirmed fact. "
    "See models.yaml's %s entry for what would actually resolve it."
)


def _provenance_for(model_name: str) -> str:
    try:
        return _PROVENANCE_BY_MODEL[model_name]
    except KeyError:
        raise KeyError(
            f"open_meteo_extractor has no provenance mapping for model '{model_name}' "
            f"(known: {sorted(_PROVENANCE_BY_MODEL)}) - add one to _PROVENANCE_BY_MODEL, "
            "matching config/models.yaml's models.open_meteo.cloud_provenance table."
        ) from None


def _format_init_dir(run_init: datetime) -> str:
    """Directory-name convention per CLAUDE.md repo layout:
    data/raw/{model}/{initYYYYMMDDHH}/ -- matches src/fetchers/base.py's
    format_init_dir() exactly; see the module-level NOTE above for why this
    is a local copy rather than an import."""
    return run_init.strftime("%Y%m%d%H")


def _target_valid_times() -> list[datetime]:
    """The eclipse-day archive valid times (e.g. 15/18/21 UTC), on
    eclipse_t()'s own calendar date -- matches src/fetchers/base.py's
    eclipse_t()/target_valid_times() exactly; see the module-level NOTE
    above for why this is a local copy rather than an import."""
    cfg = eclipse_config()
    raw = os.environ.get("ECLIPSE_T") or cfg.get("t", "2026-08-12T18:30:00Z")
    t = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    return [
        t.replace(hour=h, minute=0, second=0, microsecond=0)
        for h in cfg["archive_valid_hours_utc"]
    ]


def _forecast_path(model_name: str, run_init: datetime) -> Path:
    return DATA_RAW / model_name / _format_init_dir(run_init) / "forecast.json"


def _safe_float(seq: list, idx: int) -> float | None:
    """Value at idx as a float, or None if missing/absent/null."""
    if idx >= len(seq):
        return None
    val = seq[idx]
    return None if val is None else float(val)


@register("open_meteo_json")
def extract(model_name: str, model_config: dict, run_init: datetime) -> list[PointRow]:
    provenance = _provenance_for(model_name)
    if model_name in _UNRESOLVED_PROVENANCE_MODELS:
        logger.warning(
            _UNRESOLVED_PROVENANCE_WARNING, model_name, model_name, provenance, model_name
        )

    path = _forecast_path(model_name, run_init)
    if not path.exists():
        logger.warning("open_meteo_extractor: no forecast.json at %s -- nothing to extract", path)
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list from {path}, got {type(payload)}")

    site_list = sites()
    if len(payload) != len(site_list):
        logger.warning(
            "open_meteo_extractor: %s has %d site entries but config/sites.yaml has "
            "%d -- extracting by position anyway, verify site ordering matches",
            path,
            len(payload),
            len(site_list),
        )

    fetched_at = file_fetched_at(path)
    wanted_valid_times = _target_valid_times()
    wanted_by_key = {vt.strftime(HOURLY_VALID_TIME_FMT): vt for vt in wanted_valid_times}

    rows: list[PointRow] = []
    for site_entry, site_payload in zip(site_list, payload, strict=False):
        hourly = site_payload.get("hourly", {}) if isinstance(site_payload, dict) else {}
        times = hourly.get("time", [])
        cloud_total = hourly.get("cloud_cover", [])
        cloud_low = hourly.get("cloud_cover_low", [])
        cloud_mid = hourly.get("cloud_cover_mid", [])
        cloud_high = hourly.get("cloud_cover_high", [])

        for idx, t in enumerate(times):
            valid = wanted_by_key.get(t)
            if valid is None:
                continue  # not one of the eclipse-day archive valid times
            rows.append(
                PointRow(
                    model=model_name,
                    run_init=run_init,
                    member=-1,  # ukmo_global is deterministic
                    site=site_entry["name"],
                    valid=valid,
                    cloud_low=_safe_float(cloud_low, idx),
                    cloud_mid=_safe_float(cloud_mid, idx),
                    cloud_high=_safe_float(cloud_high, idx),
                    cloud_total=_safe_float(cloud_total, idx),
                    provenance=provenance,
                    fetched_at=fetched_at,
                )
            )

    return rows


if __name__ == "__main__":
    # Manual smoke test against whatever's already on disk, e.g.:
    #   .venv/Scripts/python.exe -m src.extract.open_meteo_extractor
    import sys

    from src.config import get_model

    _model_name = "ukmo_global"
    _model_config = get_model(_model_name)

    _init_dirs = sorted((DATA_RAW / _model_name).iterdir())
    if not _init_dirs:
        print(f"no data/raw/{_model_name}/<init>/ directories found", file=sys.stderr)
        sys.exit(1)
    _run_init = datetime.strptime(_init_dirs[-1].name, "%Y%m%d%H").replace(tzinfo=UTC)

    print(f"model={_model_name} run_init={_run_init.isoformat()}")
    _rows = extract(_model_name, _model_config, _run_init)
    print(f"extracted {len(_rows)} rows")
    for _row in _rows:
        print(_row)
