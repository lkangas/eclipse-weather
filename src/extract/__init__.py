# Importing each extractor module registers it (via @register in
# src/extract/registry.py) against its models.yaml `fetch:` key.
#
# GRIB-touching modules (grib_regular_extractor/ecmwf_extractor/icon_extractor/
# meteofrance_extractor import cfgrib transitively; aemet_extractor imports
# `src.fetchers.base`, which transitively imports herbie -> cfgrib -> eccodes
# via src/fetchers/__init__.py) only import cleanly where a native ecCodes
# install exists (i.e. inside Docker - see CLAUDE.md's deployment section).
# open_meteo_extractor.py is deliberately GRIB-free (see its own docstring)
# and meant to run standalone on a plain Windows Python venv too (T16
# backfill) - but it used to get swept into the same crash because this
# package's __init__.py imported every extractor unconditionally as one
# block. Import each module independently and log+skip ones that fail for a
# missing native dependency, so the GRIB-free modules still register. No
# behavior change where ecCodes is present (Docker): every import below
# still succeeds there exactly as before.
import logging

logger = logging.getLogger(__name__)

_EXTRACTOR_MODULES = [
    "aemet_extractor",
    "ecmwf_extractor",
    "grib_regular_extractor",
    "icon_extractor",
    "meteofrance_extractor",
    "open_meteo_extractor",
]

for _mod_name in _EXTRACTOR_MODULES:
    try:
        __import__(f"src.extract.{_mod_name}")
    except (ImportError, RuntimeError) as exc:
        logger.warning(
            "src.extract: skipping %s (import failed, likely a missing native "
            "dependency such as ecCodes): %s",
            _mod_name,
            exc,
        )

del _mod_name, _EXTRACTOR_MODULES
