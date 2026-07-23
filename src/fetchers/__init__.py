# Importing each fetcher module registers it (via @register in
# src/fetchers/registry.py) against its models.yaml `fetch:` key.
# scheduler/run.py does `from src import fetchers` purely for this side effect.
#
# GRIB-touching modules (herbie_fetcher -> herbie -> cfgrib -> eccodes) only
# import cleanly where a native ecCodes install exists (i.e. inside Docker -
# see CLAUDE.md's deployment section). On a box without one (e.g. plain
# Windows Python, no Docker/WSL) importing this package used to hard-crash
# with RuntimeError("Cannot find the ecCodes library") for EVERY fetcher,
# including open_meteo_fetcher, which has no GRIB dependency at all and is
# specifically meant to run standalone there (T16 backfill - see its own
# docstring). Import each module independently and log+skip ones that fail
# for a missing native dependency, so the GRIB-free modules still register.
# No behavior change where ecCodes is present (Docker): every import below
# still succeeds there exactly as before.
import logging

logger = logging.getLogger(__name__)

_FETCHER_MODULES = [
    "aemet_geotiff_fetcher",
    "dwd_bz2_fetcher",
    "ecmwf_opendata_fetcher",
    "herbie_fetcher",
    "meteofrance_fetcher",
    "open_meteo_fetcher",
]

for _mod_name in _FETCHER_MODULES:
    try:
        __import__(f"src.fetchers.{_mod_name}")
    except (ImportError, RuntimeError) as exc:
        logger.warning(
            "src.fetchers: skipping %s (import failed, likely a missing native "
            "dependency such as ecCodes): %s",
            _mod_name,
            exc,
        )

del _mod_name, _FETCHER_MODULES
