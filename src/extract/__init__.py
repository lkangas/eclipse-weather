# Importing each extractor module registers it (via @register in
# src/extract/registry.py) against its models.yaml `fetch:` key.
#
# NOTE: importing this package on Windows without a working ecCodes install
# will crash (grib_regular_extractor/ecmwf_extractor/icon_extractor/
# meteofrance_extractor all import cfgrib transitively) - this is expected,
# same caveat as src/fetchers/__init__.py. open_meteo_extractor.py avoids
# importing src.fetchers.base for exactly this reason; it cannot avoid being
# swept into this package's own __init__ import list, though, so importing
# `src.extract` (this package) at all requires a working eccodes (i.e. Docker).
from src.extract import (  # noqa: F401
    aemet_extractor,
    ecmwf_extractor,
    grib_regular_extractor,
    icon_extractor,
    meteofrance_extractor,
    open_meteo_extractor,
)
